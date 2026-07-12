#!/bin/bash
# Live IPTV station switcher for FluxRT SD-Turbo stream
# Usage: ./switch_station.sh <station-name>
#        ./switch_station.sh list

CONTROL_PORT=8889

declare -A STATIONS=(
  # --- Jazz / Soul / Blues ---
  [smooth-jazz]="https://lotus.stingray.com/manifest/ose-140ads-montreal/samsungtvplus/master.m3u8"
  [soul-storm]="https://lotus.stingray.com/manifest/ose-134ads-montreal/samsungtvplus/master.m3u8"
  [djazz]="https://lotus.stingray.com/manifest/djazz-djaads-montreal/samsungtvplus/master.m3u8"
  [jazz-beyond]="https://samsungau-qwestjazz-samsungtv-zw2jc.amagi.tv/playlist/samsungau-qwestjazz-samsungtv/playlist.m3u8"
  [vevo-rb]="https://d1hf773q57zx9s.cloudfront.net/Vevo_R_B.m3u8"

  # --- Techno / Dance / EDM ---
  [techno-warehouse]="https://m2b2.worldcast.tv:7443/dancetelevisionthree/dancetelevisionthree.m3u8"
  [minimal-tech]="https://mbit1.worldcast.tv/dancetelevisionsix/multibit.m3u8"
  [edm]="https://mbit1.worldcast.tv/dancetelevisionseven/multibit.m3u8"
  [house-floor]="https://m2b2.worldcast.tv:7443/dancetelevisionfive/dancetelevisionfive.m3u8"
  [deep-house]="https://m1b2.worldcast.tv/dancetelevisiontwo/dancetelevisiontwo.m3u8"

  # --- Original / Misc ---
  [glewedtv-11]="https://linear-11.frequency.stream/dist/glewedtv/11/hls/master/playlist.m3u8"
  [glewedtv-188]="https://linear-188.frequency.stream/dist/glewedtv/188/hls/master/playlist.m3u8"
)

if [[ "$1" == "list" || -z "$1" ]]; then
  echo "Available stations:"
  echo ""
  echo "  Jazz / Soul / Blues:"
  echo "    smooth-jazz       Stingray Smooth Jazz"
  echo "    soul-storm        Stingray Soul Storm"
  echo "    djazz             Stingray DJAZZ"
  echo "    jazz-beyond       Qwest Jazz Beyond"
  echo "    vevo-rb           Vevo R&B"
  echo ""
  echo "  Techno / Dance / EDM:"
  echo "    techno-warehouse  DanceTV Techno Warehouse"
  echo "    minimal-tech      DanceTV Minimal Tech"
  echo "    edm               DanceTV EDM"
  echo "    house-floor       DanceTV House Floor"
  echo "    deep-house        DanceTV Deep House"
  echo ""
  echo "  Original / Misc:"
  echo "    glewedtv-11       GlewedTV Channel 11"
  echo "    glewedtv-188      GlewedTV Channel 188"
  echo ""
  echo "Usage: $0 <station-name>"
  echo "Example: $0 techno-warehouse"
  exit 0
fi

STATION="$1"
URL="${STATIONS[$STATION]}"

if [[ -z "$URL" ]]; then
  echo "Unknown station: $STATION"
  echo "Run '$0 list' to see available stations"
  exit 1
fi

echo "Switching to: $STATION"
echo "URL: $URL"
curl -s -X POST "http://localhost:${CONTROL_PORT}/channel" \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"${URL}\"}"
echo ""
echo "Done! Station switched live — no restart needed."
