#!/usr/bin/env bash
# Checks the Realtime Database security rules against the live database (E5 evidence).
# Usage:  FB_PASSWORD='gateway-password' bash firebase/test_rules.sh
# Leaves one entry under history/TEST (history is append-only); delete it in the console.
KEY=AIzaSyBbp_xiCoqc7g7XkslIRC-fCTK81NOIZ2w   # web API key, not a secret
DB=https://airtomic-29e37-default-rtdb.asia-southeast1.firebasedatabase.app
: "${FB_PASSWORD:?set FB_PASSWORD to the gateway account password}"

TOK=$(curl -s -X POST "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=$KEY" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"gateway@airtomic.app\",\"password\":\"$FB_PASSWORD\",\"returnSecureToken\":true}" |
  python -c "import sys,json;print(json.load(sys.stdin)['idToken'])") || { echo "sign-in failed"; exit 1; }

fails=0
t() { # name expected-status method path body [anon]
  local u="$DB/$4.json"; [ "$6" != "anon" ] && u="$u?auth=$TOK"
  local code; code=$(curl -s -o /tmp/fb_body -w "%{http_code}" -X "$3" "$u" ${5:+-d "$5"})
  if [ "$code" = "$2" ]; then echo "pass  $1"; else echo "FAIL  $1 (got $code, want $2)"; fails=$((fails+1)); fi
}
REC='{"gw":"GW01","node_id":"SN01","seq":0,"state":"ABSENT","ac":"ON","light":"ON","ts":{".sv":"timestamp"}}'

t "gateway writes latest nodes/SN01"        200 PUT    nodes/SN01 "$REC"
t "gateway reads nodes"                     200 GET    nodes ""
t "gateway appends history/TEST"            200 POST   history/TEST "$REC"
echo "      new history id: $(cat /tmp/fb_body)"
PID=$(sed -nE 's/.*"name" *: *"([^"]+)".*/\1/p' /tmp/fb_body)
[ -n "$PID" ] || { echo "FAIL  could not read new history id"; exit 1; }
t "gateway overwrites history entry (deny)" 401 PUT    "history/TEST/$PID" "$REC"
t "record without seq (deny)"               401 PUT    nodes/SN01 '{"node_id":"SN01"}'
t "cmd ac=OFF"                              200 PUT    cmd/SN01/ac '"OFF"'
t "gateway reads cmd"                       200 GET    cmd/SN01 ""
t "gateway deletes cmd (ack)"               200 DELETE cmd/SN01/ac ""
t "cmd ac=ON (deny)"                        401 PUT    cmd/SN01/ac '"ON"'
t "cmd fan=OFF (deny)"                      401 PUT    cmd/SN01/fan '"OFF"'
t "gateway edits operators (deny)"          401 PUT    operators/x 'true'
t "anonymous read nodes (deny)"             401 GET    nodes "" anon
t "anonymous cmd (deny)"                    401 PUT    cmd/SN01/ac '"OFF"' anon
t "anonymous write nodes (deny)"            401 PUT    nodes/SN01 "$REC" anon
t "cleanup: delete nodes/SN01"              200 DELETE nodes/SN01 ""

echo "$fails failure(s)"; exit $fails
