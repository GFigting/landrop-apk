#!/usr/bin/env bash
cd "$(dirname "$0")"
URL="https://gfigting.github.io/landrop-apk/LANDrop-arm64.apk"
TOTAL=33174343
N=8
CHUNK=$(( (TOTAL + N - 1) / N ))
download_chunk() {
  i=$1
  start=$(( i * CHUNK ))
  end=$(( start + CHUNK - 1 ))
  [ $end -ge $TOTAL ] && end=$(( TOTAL - 1 ))
  want=$(( end - start + 1 ))
  part="_chunk_$i.part"
  while true; do
    have=$(stat -c %s "$part" 2>/dev/null || echo 0)
    [ "$have" -ge "$want" ] && break
    curl -s -r $(( start + have ))-$end -o - "$URL" --max-time 100 >> "$part" 2>/dev/null
    sleep 1
  done
  echo "chunk $i done ($want bytes)"
}
for i in 0 1 2 3 4 5 6 7; do download_chunk $i & done
wait
CHUNKS=""
for i in 0 1 2 3 4 5 6 7; do CHUNKS="$CHUNKS _chunk_$i.part"; done
cat $CHUNKS > LANDrop-arm64.apk
echo "FINAL: $(stat -c %s LANDrop-arm64.apk)"
