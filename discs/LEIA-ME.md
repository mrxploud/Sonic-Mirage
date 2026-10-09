# Discos

Este projeto **não inclui** os jogos. Você precisa das suas próprias cópias, nestas versões exatas
(os endereços de memória usados pelo código só valem para elas):

| Jogo | Console | Versão |
|---|---|---|
| Silhouette Mirage | Sega Saturn, japonês | T-32901G V1.003 (30/07/1997) |
| Sonic the Hedgehog CD | Sega CD, americano | MK-4407 |

O programa espera encontrar:

```
discs/
  silhouette_mirage/silhouette_mirage.cue   (+ track01.iso, track02.bin, ...)
  sonic_cd/sonic_cd.cue                     (+ track01.iso, track02.bin, ...)
```

Se a sua cópia estiver no formato "um `.iso` mais as faixas de áudio em `.mp3`", o script abaixo
converte para esse formato (precisa do `ffmpeg`):

```
python -I tools/build_disc.py "pasta/do/Silhouette Mirage" discs/silhouette_mirage
python -I tools/build_disc.py "pasta/do/Sonic CD" discs/sonic_cd
```

No Silhouette Mirage o script recoloca cada faixa de áudio na posição exata do disco, lida do
próprio sistema de arquivos do jogo. Isso importa: o jogo toca as músicas por posição, e um `.cue`
com as faixas deslocadas faz a música entrar no lugar errado.
