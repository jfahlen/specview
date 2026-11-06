#!/usr/bin/env bash
# Drive the Phase 0 spike against a deployed server.
#   BASE=https://specview-api.onrender.com SPIKE_TOKEN=... scripts/spike.sh [lat lon]
set -euo pipefail
: "${BASE:?set BASE}" "${SPIKE_TOKEN:?set SPIKE_TOKEN}"
LAT=${1:-34.20}; LON=${2:--118.17}
H="Authorization: Bearer $SPIKE_TOKEN"
j() { python3 -m json.tool; }

echo "== RTT (/spike/ping, 20 requests, one keep-alive connection)"
urls=(); for _ in $(seq 20); do urls+=("$BASE/spike/ping" -o /dev/null); done
curl -s --http2 -w "%{time_total}\n" "${urls[@]}" | sort -n | awk '{a[NR]=$1} END{printf "min %.0f ms  median %.0f ms  max %.0f ms\n",a[1]*1000,a[int((NR+1)/2)]*1000,a[NR]*1000}'

echo "== whoami";  curl -s -H "$H" "$BASE/spike/whoami" | j
echo "== search";  S=$(curl -s -H "$H" "$BASE/spike/search?lat=$LAT&lon=$LON&count=5"); echo "$S" | j
URL=$(echo "$S" | python3 -c 'import sys,json; g=[x for x in json.load(sys.stdin)["granules"] if x["s3"]]; print(g[0]["s3"])')
echo "granule: $URL"
echo "== layout";     curl -s -H "$H" "$BASE/spike/emit/layout" --get --data-urlencode "s3=$URL" | j
echo "== pixel x3";   for _ in 1 2 3; do curl -s -H "$H" "$BASE/spike/emit/pixel?row=600&col=600" --get --data-urlencode "s3=$URL"; echo; done
echo "== throughput"; curl -s -H "$H" "$BASE/spike/emit/throughput?mb=512" --get --data-urlencode "s3=$URL" | j
echo "== full read (prepare proxy)"; curl -s -m 600 -H "$H" "$BASE/spike/emit/full" --get --data-urlencode "s3=$URL" | j
