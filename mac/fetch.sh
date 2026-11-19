#!/bin/sh
# fetch.sh: download one release of mac-local-llm-kit, check every piece, rebuild the original files.
#
#   sh fetch.sh <tag> <dest-dir> [work-dir]
#   sh fetch.sh cfg1-qwen3.8-27b-q5_k_s-gguf ~/models/cfg1-qwen3.8-27b-q5_k_s-gguf
#
# 1. Downloads MANIFEST.tsv from the release. It is attached last and the release is a draft until
#    every asset is up, so "no MANIFEST.tsv" means the release is not published yet.
# 2. Downloads every asset into <work-dir> (default ~/llm-kit/downloads/<tag>), resuming partial
#    files, and checks each one's size and sha256 against the manifest.
# 3. Rebuilds each file in <dest-dir>: a split file is name.part001 + name.part002 + ... in order
#    (each part is deleted right after it is appended, so the disk never holds two copies);
#    a whole file is moved over.
# 4. Checks the rebuilt file's size and sha256: for weights this is the sha256 Hugging Face
#    publishes, so a match means the file is byte-identical to the upstream one.
# 5. Removes <work-dir>.
# Rerun the same command after any interruption: verified files are skipped, downloads resume.
# Uses only what macOS ships: sh, curl, shasum, awk, df.
set -eu

REPO=${KIT_REPO:-CharlesGuooo/mac-local-llm-kit}
usage="usage: sh fetch.sh <tag> <dest-dir> [work-dir]"
TAG=${1:?$usage}
DEST=${2:?$usage}
WORK=${3:-$HOME/llm-kit/downloads/$TAG}
BASE="https://github.com/$REPO/releases/download/$TAG"
TAB=$(printf '\t')

die() { echo "ERROR: $*" >&2; exit 1; }
bytes() { wc -c < "$1" | tr -d ' '; }
sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else sha256sum "$1" | awk '{print $1}'; fi
}
check() { # check <path> <bytes> <sha256>: true when the file exists with this size and hash
  [ -f "$1" ] && [ "$(bytes "$1")" = "$2" ] && [ "$(sha256 "$1")" = "$3" ]
}

fetch_asset() { # fetch_asset <name> <bytes> <sha256>
  out="$WORK/$1"
  if check "$out" "$2" "$3"; then echo "  have  $1"; return 0; fi
  tries=0
  while :; do
    tries=$((tries + 1))
    [ "$tries" -le 8 ] || die "$1 still does not match the manifest after 8 attempts"
    have=0
    [ -f "$out" ] && have=$(bytes "$out")
    if [ "$have" -gt "$2" ]; then rm -f "$out"; have=0; fi
    if [ "$have" -lt "$2" ]; then
      echo "  get   $1 ($(($2 / 1048576)) MiB, $(($have / 1048576)) MiB already here)"
      if ! curl -fsSL --retry 10 --retry-delay 10 --retry-all-errors --connect-timeout 30 \
           -C - -o "$out" "$BASE/$1" </dev/null; then
        echo "  curl stopped on $1 (attempt $tries), resuming in 30 s" >&2
        sleep 30
        continue
      fi
    fi
    if check "$out" "$2" "$3"; then echo "  ok    $1"; return 0; fi
    echo "  bad   $1 (size or sha256 differs from the manifest), downloading it again" >&2
    rm -f "$out"
  done
}

mkdir -p "$WORK" "$DEST"
echo "== $TAG"
echo "   from $BASE"
echo "   work $WORK"
echo "   into $DEST"
curl -fsSL --retry 5 --connect-timeout 30 -o "$WORK/MANIFEST.tsv" "$BASE/MANIFEST.tsv" </dev/null ||
  die "no MANIFEST.tsv at $BASE: release not published yet, or wrong tag. Published ones: https://github.com/$REPO/releases"
grep "^asset$TAB" "$WORK/MANIFEST.tsv" > "$WORK/assets.tsv" || die "MANIFEST.tsv lists no assets"
grep "^file$TAB" "$WORK/MANIFEST.tsv" > "$WORK/files.tsv" || die "MANIFEST.tsv lists no files"

done_all=yes
while IFS="$TAB" read -r kind name size sha parts; do
  check "$DEST/$name" "$size" "$sha" || { done_all=no; break; }
done < "$WORK/files.tsv"
if [ "$done_all" = yes ]; then
  rm -rf "$WORK"
  echo "== already complete: every file in $DEST matches the manifest"
  exit 0
fi

remaining=0
while IFS="$TAB" read -r kind name size sha; do
  have=0
  [ -f "$WORK/$name" ] && have=$(bytes "$WORK/$name")
  [ "$have" -le "$size" ] || have=0
  remaining=$((remaining + size - have))
done < "$WORK/assets.tsv"
avail_kb=$(df -Pk "$WORK" | awk 'NR == 2 {print $4}')
need_kb=$((remaining / 1024 + 2 * 1024 * 1024))
[ "$avail_kb" -ge "$need_kb" ] ||
  die "not enough disk: about $((need_kb / 1048576)) GiB must be free in $WORK, only $((avail_kb / 1048576)) GiB is"

echo "-- downloading $(awk 'END {print NR}' "$WORK/assets.tsv") assets, $((remaining / 1048576)) MiB still to fetch"
while IFS="$TAB" read -r kind name size sha; do
  fetch_asset "$name" "$size" "$sha"
done < "$WORK/assets.tsv"

echo "-- rebuilding files in $DEST"
while IFS="$TAB" read -r kind name size sha parts; do
  final="$DEST/$name"
  if check "$final" "$size" "$sha"; then echo "  have  $name"; continue; fi
  tmp="$final.partial"
  if [ "$parts" -eq 0 ]; then
    mv -f "$WORK/$name" "$tmp"
  else
    : > "$tmp"
    i=1
    while [ "$i" -le "$parts" ]; do
      p="$WORK/$(printf '%s.part%03d' "$name" "$i")"
      [ -f "$p" ] || die "missing $p; run the same command again to download it"
      cat "$p" >> "$tmp"
      rm -f "$p"
      i=$((i + 1))
    done
  fi
  if check "$tmp" "$size" "$sha"; then
    mv -f "$tmp" "$final"
    echo "  built $name ($parts parts, sha256 ok)"
  else
    rm -f "$tmp"
    die "$name does not match its sha256 after rebuilding; run the same command again to re-download"
  fi
done < "$WORK/files.tsv"

rm -rf "$WORK"
echo "== done: $TAG is complete in $DEST"
