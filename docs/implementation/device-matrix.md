# Physical-device evidence matrix

Work item: https://github.com/phaabe/live.moafunk.de/issues/339
Requirements: [F7 device gates](frontend-v4.md#f7--qualify-native-devices-and-approve-the-frontend-release-profile).

Status: no physical-device qualification recorded here. Device availability
and the supported minimum OS remain operator decisions in the shared P1/O1.1
inventory. These rows are evidence slots, not a supported-platform promise.

| Candidate path | Required environment detail | Evidence status |
| --- | --- | --- |
| iPhone/iPad Safari, MP3 | Physical model; exact OS/browser; normal tab; speaker/headphones; Wi-Fi/cellular | Not run; device and operator pending. |
| iPhone/iPad native HLS | Same fields plus exact allowlisted profile and O4 harness evidence | Blocked until HLS and F7 prerequisites pass. |
| Home-screen mode | Separate device/OS run and lifecycle observations | Not run; no support claim. |
| AirPlay/CarPlay | Separate receiver/device/OS and route-switch evidence | Not run; no support claim. |
| Android browser, MP3 | Model, OS/browser, network and output route | Not run. |
| Desktop browser, MP3 | OS/browser, audio route and network | Not run. |
| VLC/direct URL/ICY | Client version, exact endpoint and observed metadata/audio | Not run. |

## One record per device, artifact and route

Record the source commit, frontend artifact/configuration ID, media image/config
identity, endpoint, physical model, OS/browser versions, mode, network, route,
date, observer and evidence URL. Redact credentials and private programme data.
Use the same scenarios before and after a change. Keep failed and unsupported
cases in the matrix rather than dropping them from the totals.

| Scenario | Acceptance/evidence required by F7 |
| --- | --- |
| Startup | Individual tap-to-audio samples, sample count, timing method and p95; target under five seconds on a healthy network. |
| Short network loss | At least 19/20 recoveries within fifteen seconds of connectivity return, per supported path; list failures. |
| End-to-end delay | Producer-to-ear measurement; HLS delay over thirty seconds or degraded live chat needs a recorded decision. |
| Locked playback | Thirty-minute run; programme/cover change while locked; record stale lock-screen values honestly. |
| Interruptions | Wi-Fi↔cellular, loss while locked, long pause/resume, call/Siri and headphone removal; no unintended restart after pause. |
| Release and recovery | Deploy during buffering, same-commit configuration rollback, HLS→MP3 fallback and rejected autoplay/tap-to-resume. |
| Media faults | Graceful restart/crash, source handover and API replacement; correlate device observations with output generations and decode probes. |
| Sustained operation | At least two full shows when required by the release gates; distinguish producer failure from station delivery failure. |

Result values: not run, blocked (reason), pass (evidence), fail (evidence), or
unsupported (recorded decision). A merged PR, successful HTTP request, jsdom
test or user-agent emulation is never counted as a physical-device pass. Keep
MP3 as the supported default unless the required native-HLS gates pass and the
operator approves the exact release profile.
