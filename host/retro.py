"""Minimal libretro frontend: load a core DLL, run frames, feed input, grab video and RAM.

Several Core objects (one per *different* DLL) can live in the same process, which is
what the fusion host relies on: both games run in lockstep and share one address space.
"""
import ctypes as C
import os
import re

import numpy as np

# RetroPad button ids
B, Y, SELECT, START, UP, DOWN, LEFT, RIGHT, A, X, L, R, L2, R2, L3, R3 = range(16)
BUTTONS = dict(B=B, Y=Y, SELECT=SELECT, START=START, UP=UP, DOWN=DOWN, LEFT=LEFT, RIGHT=RIGHT,
               A=A, X=X, L=L, R=R, L2=L2, R2=R2, L3=L3, R3=R3)

DEVICE_JOYPAD = 1
MEMORY_SAVE_RAM, MEMORY_RTC, MEMORY_SYSTEM_RAM, MEMORY_VIDEO_RAM = range(4)
PIXEL_0RGB1555, PIXEL_XRGB8888, PIXEL_RGB565 = range(3)

ENV_EXPERIMENTAL = 0x10000
(ENV_GET_OVERSCAN, ENV_GET_CAN_DUPE, ENV_SET_MESSAGE, ENV_GET_SYSTEM_DIRECTORY, ENV_SET_PIXEL_FORMAT,
 ENV_SET_INPUT_DESCRIPTORS, ENV_GET_VARIABLE, ENV_SET_VARIABLES, ENV_GET_VARIABLE_UPDATE,
 ENV_GET_LOG_INTERFACE, ENV_GET_CORE_ASSETS_DIRECTORY, ENV_GET_SAVE_DIRECTORY, ENV_SET_MEMORY_MAPS,
 ENV_GET_LANGUAGE, ENV_SET_MESSAGE_EXT) = 2, 3, 6, 9, 10, 11, 15, 16, 17, 27, 30, 31, 36, 39, 60


class GameInfo(C.Structure):
    _fields_ = [('path', C.c_char_p), ('data', C.c_void_p), ('size', C.c_size_t), ('meta', C.c_char_p)]


class SystemInfo(C.Structure):
    _fields_ = [('library_name', C.c_char_p), ('library_version', C.c_char_p),
                ('valid_extensions', C.c_char_p), ('need_fullpath', C.c_bool), ('block_extract', C.c_bool)]


class Geometry(C.Structure):
    _fields_ = [('base_width', C.c_uint), ('base_height', C.c_uint), ('max_width', C.c_uint),
                ('max_height', C.c_uint), ('aspect_ratio', C.c_float)]


class Timing(C.Structure):
    _fields_ = [('fps', C.c_double), ('sample_rate', C.c_double)]


class AVInfo(C.Structure):
    _fields_ = [('geometry', Geometry), ('timing', Timing)]


class Variable(C.Structure):
    _fields_ = [('key', C.c_char_p), ('value', C.c_char_p)]


class Message(C.Structure):
    _fields_ = [('msg', C.c_char_p), ('frames', C.c_uint)]


class InputDescriptor(C.Structure):
    _fields_ = [('port', C.c_uint), ('device', C.c_uint), ('index', C.c_uint), ('id', C.c_uint),
                ('description', C.c_char_p)]


class MemoryDescriptor(C.Structure):
    _fields_ = [('flags', C.c_uint64), ('ptr', C.c_void_p), ('offset', C.c_size_t), ('start', C.c_size_t),
                ('select', C.c_size_t), ('disconnect', C.c_size_t), ('len', C.c_size_t),
                ('addrspace', C.c_char_p)]


class MemoryMap(C.Structure):
    _fields_ = [('descriptors', C.POINTER(MemoryDescriptor)), ('num_descriptors', C.c_uint)]


ENV_CB = C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
VIDEO_CB = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
AUDIO_CB = C.CFUNCTYPE(None, C.c_int16, C.c_int16)
AUDIO_BATCH_CB = C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
POLL_CB = C.CFUNCTYPE(None)
STATE_CB = C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)
# retro_log_printf_t is variadic; on Win64 every variadic argument also travels in an
# integer slot, so the trailing arguments are taken as raw 64-bit words and formatted here.
LOG_CB = C.CFUNCTYPE(None, C.c_int, C.c_char_p, *([C.c_uint64] * 8))


class LogCallback(C.Structure):
    _fields_ = [('log', LOG_CB)]


def _format_log(fmt, args):
    args = list(args)

    def sub(m):
        if m.group(0) == '%%':
            return '%'
        if not args:
            return m.group(0)
        v = args.pop(0)
        conv = m.group(2)
        try:
            if conv == 's':
                return C.string_at(v).decode('utf-8', 'replace') if v else '(null)'
            if conv in 'fFgGeE':
                return '%g' % C.c_double.from_buffer(C.c_uint64(v)).value
            if conv in 'di':
                wide = 'll' in m.group(1) or 'z' in m.group(1)
                return str(C.c_int64(v).value if wide else C.c_int32(v & 0xFFFFFFFF).value)
            if conv in 'xXp':
                return '%x' % (v if ('l' in m.group(1) or conv == 'p') else v & 0xFFFFFFFF)
            if conv == 'c':
                return chr(v & 0xFF)
            return str(v if 'l' in m.group(1) or 'z' in m.group(1) else v & 0xFFFFFFFF)
        except (OSError, ValueError):
            return '?'

    return re.sub(r'%%|%([-+ #0-9.lhzIj]*)([a-zA-Z])', sub, fmt)


class Region:
    """A live view of one block of emulated memory."""

    def __init__(self, name, start, ptr, length, swap16=False):
        self.name, self.start, self.len, self.swap16 = name, start, length, swap16
        self.raw = np.ctypeslib.as_array((C.c_ubyte * length).from_address(ptr))

    def snapshot(self):
        """Copy of the block in the guest's own byte order."""
        b = self.raw.copy()
        if self.swap16:
            b = b.reshape(-1, 2)[:, ::-1].reshape(-1)
        return b

    def _host(self, addr, n):
        o = addr - self.start
        if o < 0 or o + n > self.len:
            raise IndexError('%s: address %X out of range' % (self.name, addr))
        return o

    def read(self, addr, n):
        o = self._host(addr, n)
        if not self.swap16:
            return bytes(self.raw[o:o + n])
        return bytes(self.raw[(o + k) ^ 1] for k in range(n))

    def write(self, addr, data):
        o = self._host(addr, len(data))
        for k, v in enumerate(data):
            self.raw[(o + k) ^ 1 if self.swap16 else o + k] = v

    def u8(self, a):
        return self.read(a, 1)[0]

    def u16(self, a):
        return int.from_bytes(self.read(a, 2), 'big')

    def u32(self, a):
        return int.from_bytes(self.read(a, 4), 'big')

    def s16(self, a):
        return int.from_bytes(self.read(a, 2), 'big', signed=True)

    def s32(self, a):
        return int.from_bytes(self.read(a, 4), 'big', signed=True)

    def w8(self, a, v):
        self.write(a, bytes([v & 0xFF]))

    def w16(self, a, v):
        self.write(a, (int(v) & 0xFFFF).to_bytes(2, 'big'))

    def w32(self, a, v):
        self.write(a, (int(v) & 0xFFFFFFFF).to_bytes(4, 'big'))


class Core:
    def __init__(self, dll, system_dir, save_dir, options=None, log=None):
        self.name = os.path.basename(dll)
        self.options = {k.encode(): v.encode() for k, v in (options or {}).items()}
        self.variables = {}
        self.descriptors = []
        self.memory_map = []
        self.messages = []
        self.pixel_format = PIXEL_0RGB1555
        self.pressed = set()
        self.frame = None
        self.frames = 0
        self.audio = None
        self._log = log
        os.makedirs(save_dir, exist_ok=True)
        self._system_dir = C.c_char_p(os.path.abspath(system_dir).encode())
        self._save_dir = C.c_char_p(os.path.abspath(save_dir).encode())

        self._cbs = (ENV_CB(self._environment), VIDEO_CB(self._video), AUDIO_CB(lambda l, r: None),
                     AUDIO_BATCH_CB(self._audio_batch), POLL_CB(lambda: None), STATE_CB(self._input_state),
                     LOG_CB(self._log_printf))
        self.lib = lib = C.CDLL(os.path.abspath(dll))
        lib.retro_load_game.argtypes = [C.POINTER(GameInfo)]
        lib.retro_load_game.restype = C.c_bool
        lib.retro_get_memory_data.argtypes = [C.c_uint]
        lib.retro_get_memory_data.restype = C.c_void_p
        lib.retro_get_memory_size.argtypes = [C.c_uint]
        lib.retro_get_memory_size.restype = C.c_size_t
        lib.retro_serialize_size.restype = C.c_size_t
        lib.retro_serialize.argtypes = [C.c_void_p, C.c_size_t]
        lib.retro_serialize.restype = C.c_bool
        lib.retro_unserialize.argtypes = [C.c_void_p, C.c_size_t]
        lib.retro_unserialize.restype = C.c_bool
        lib.retro_set_controller_port_device.argtypes = [C.c_uint, C.c_uint]
        for fn in ('retro_init', 'retro_deinit', 'retro_run', 'retro_reset', 'retro_unload_game',
                   'retro_get_system_info', 'retro_get_system_av_info'):
            getattr(lib, fn).restype = None

        lib.retro_set_environment(self._cbs[0])
        lib.retro_set_video_refresh(self._cbs[1])
        lib.retro_set_audio_sample(self._cbs[2])
        lib.retro_set_audio_sample_batch(self._cbs[3])
        lib.retro_set_input_poll(self._cbs[4])
        lib.retro_set_input_state(self._cbs[5])
        lib.retro_init()
        info = SystemInfo()
        lib.retro_get_system_info(C.byref(info))
        self.library = '%s %s' % (info.library_name.decode(), info.library_version.decode())
        self.need_fullpath = info.need_fullpath

    # --- callbacks -------------------------------------------------------------------
    def _log_printf(self, level, fmt, *args):
        if self._log and fmt:
            self._log(level, _format_log(fmt.decode('utf-8', 'replace'), args).rstrip())

    def _environment(self, cmd, data):
        cmd &= ~ENV_EXPERIMENTAL
        if cmd == ENV_GET_CAN_DUPE:
            C.cast(data, C.POINTER(C.c_bool))[0] = True
        elif cmd == ENV_GET_OVERSCAN:
            C.cast(data, C.POINTER(C.c_bool))[0] = False
        elif cmd in (ENV_GET_SYSTEM_DIRECTORY, ENV_GET_CORE_ASSETS_DIRECTORY):
            C.cast(data, C.POINTER(C.c_char_p))[0] = self._system_dir
        elif cmd == ENV_GET_SAVE_DIRECTORY:
            C.cast(data, C.POINTER(C.c_char_p))[0] = self._save_dir
        elif cmd == ENV_SET_PIXEL_FORMAT:
            self.pixel_format = C.cast(data, C.POINTER(C.c_int))[0]
        elif cmd == ENV_GET_LOG_INTERFACE:
            C.cast(data, C.POINTER(LogCallback))[0].log = self._cbs[6]
        elif cmd == ENV_GET_LANGUAGE:
            C.cast(data, C.POINTER(C.c_uint))[0] = 0
        elif cmd == ENV_GET_VARIABLE_UPDATE:
            C.cast(data, C.POINTER(C.c_bool))[0] = False
        elif cmd == ENV_GET_VARIABLE:
            var = C.cast(data, C.POINTER(Variable))[0]
            value = self.options.get(var.key)
            if value is None:
                return False
            var.value = value
        elif cmd == ENV_SET_VARIABLES:
            v = C.cast(data, C.POINTER(Variable))
            i = 0
            while v[i].key:
                self.variables[v[i].key.decode()] = (v[i].value or b'').decode('utf-8', 'replace')
                i += 1
        elif cmd == ENV_SET_INPUT_DESCRIPTORS:
            d = C.cast(data, C.POINTER(InputDescriptor))
            i = 0
            self.descriptors = []
            while d[i].description:
                self.descriptors.append((d[i].port, d[i].device, d[i].index, d[i].id, d[i].description.decode()))
                i += 1
        elif cmd == ENV_SET_MEMORY_MAPS:
            mm = C.cast(data, C.POINTER(MemoryMap))[0]
            self.memory_map = [dict(flags=d.flags, ptr=d.ptr, start=d.start, len=d.len, offset=d.offset,
                                    select=d.select, name=(d.addrspace or b'').decode())
                               for d in (mm.descriptors[i] for i in range(mm.num_descriptors))]
        elif cmd in (ENV_SET_MESSAGE, ENV_SET_MESSAGE_EXT):
            msg = C.cast(data, C.POINTER(Message))[0].msg
            if msg:
                self.messages.append(msg.decode('utf-8', 'replace'))
                if self._log:
                    self._log(1, '[message] ' + self.messages[-1])
        else:
            return False
        return True

    def _video(self, data, width, height, pitch):
        if data:
            self.frame = (C.string_at(data, pitch * height), width, height, pitch)

    def _audio_batch(self, data, frames):
        if self.audio is not None:
            self.audio.append(C.string_at(data, frames * 4))
        return frames

    def _input_state(self, port, device, index, button):
        return 1 if port == 0 and device == DEVICE_JOYPAD and button in self.pressed else 0

    # --- control ---------------------------------------------------------------------
    def load(self, path):
        path = os.path.abspath(path)
        self._game_path = path.encode()
        info = GameInfo(self._game_path, None, 0, None)
        if not self.need_fullpath:
            with open(path, 'rb') as f:
                self._game_data = f.read()
            info.data = C.cast(C.c_char_p(self._game_data), C.c_void_p)
            info.size = len(self._game_data)
        if not self.lib.retro_load_game(C.byref(info)):
            raise RuntimeError('%s refused to load %s' % (self.name, path))
        av = AVInfo()
        self.lib.retro_get_system_av_info(C.byref(av))
        self.fps, self.sample_rate = av.timing.fps, av.timing.sample_rate
        self.lib.retro_set_controller_port_device(0, DEVICE_JOYPAD)

    def run(self, frames=1, pressed=None):
        if pressed is not None:
            self.pressed = set(pressed)
        for _ in range(frames):
            self.lib.retro_run()
            self.frames += 1

    def reset(self):
        self.lib.retro_reset()

    def close(self):
        self.lib.retro_unload_game()
        self.lib.retro_deinit()

    def save_state(self):
        n = self.lib.retro_serialize_size()
        buf = C.create_string_buffer(n)
        if not self.lib.retro_serialize(buf, n):
            raise RuntimeError('serialize failed')
        return buf.raw

    def load_state(self, state):
        if not self.lib.retro_unserialize(state, len(state)):
            raise RuntimeError('unserialize failed')

    # --- video -----------------------------------------------------------------------
    def image(self):
        """Last video frame as an (h, w, 3) uint8 RGB array."""
        data, w, h, pitch = self.frame
        if self.pixel_format == PIXEL_XRGB8888:
            a = np.frombuffer(data, np.uint8).reshape(h, pitch // 4, 4)[:, :w, 2::-1]
            return np.ascontiguousarray(a)
        v = np.frombuffer(data, '<u2').reshape(h, pitch // 2)[:, :w].astype(np.uint32)
        if self.pixel_format == PIXEL_RGB565:
            r, g, b = (v >> 11) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31
        else:
            r, g, b = (v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v & 31) * 255 // 31
        return np.dstack([r, g, b]).astype(np.uint8)

    # --- memory ----------------------------------------------------------------------
    def memory(self, mem_id):
        ptr = self.lib.retro_get_memory_data(mem_id)
        size = self.lib.retro_get_memory_size(mem_id)
        return (ptr, size) if ptr and size else None

    def region(self, name, start, ptr, length, swap16=False):
        return Region(name, start, ptr, length, swap16)
