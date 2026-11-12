#!/bin/sh
# Render every Graphviz source in this folder to SVG (what the docs embed), and optionally PNG.
#
#   docs/diagrams/render.sh          # *.dot → *.svg
#   docs/diagrams/render.sh --png    # also *.png (150 dpi), e.g. to check a layout by eye
#
# Needs Graphviz ≥ 2.43 (apt: graphviz) and the Noto Sans font (apt: fonts-noto-core). Edit the .dot, rerun, commit
# both. Colours mean the same thing in every diagram (see the comment at the top of overview.dot).
set -eu
cd "$(dirname "$0")"
for f in *.dot; do
  dot -Tsvg "$f" -o "${f%.dot}.svg"
  if [ "${1:-}" = "--png" ]; then dot -Tpng -Gdpi=150 "$f" -o "${f%.dot}.png"; fi
  echo "rendered ${f%.dot}"
done
