# YANDI Transport Updates

## Summary

Short record of the transport changes completed in the current iteration.

## Completed

- Fixed browser/media file handling in chat:
  - upload chunking aligned with backend
  - unstable inline playback replaced with open-in-new-tab flow
  - download kept intact
- Fixed chat UX:
  - messages render in normal bottom-growing order
  - pending file upload progress moved into chat bubbles
- Hardened `netlayer` port rotation:
  - fallback `9000/10000` always open
  - active rotated listeners spawned correctly
  - previous rotated ports survive grace TTL and then close
  - peers receive and apply `PortUpdate`
  - heartbeat and stream paths follow active sockets
- Improved depot stability:
  - completed trains cleaned by TTL
  - memory accounting fixed
  - clone memory included
  - max train pressure reduced by larger depot limits
- Reduced proxy log noise:
  - dead tunnels removed from `active_tunnels`
  - repeated writes to closed browser/server sockets stopped
  - normal teardown no longer floods logs with `Broken pipe`
- Reworked transport metrics on the main page:
  - local `TX raw`
  - local `RX raw`
  - `Peer RX`
  - `Peer Path0`
  - local `Path0 loss`
  - `Clone hits`
  - `Wagons/s`
  - `Depot`
  - `Active trains`
  - `Evict`
  - `Drop+CRC`
  - `Retrans`
  - RTT from active streams

## Practical Result

- Peak-hour proxying became noticeably more stable.
- Long-lived media flows (`YouTube 1080p60`, streaming sites, `x.com`) now hold much better.
- Port rotation works in field testing without tearing down active traffic.

## Still Pending

- adaptive pacing
- bounded dynamic wagon sizing
- explicit effective loss metric
- duplicate-dropped metric in UI
- useful throughput metric
- hardened `p2p` transport v2 with the same station/train/wagon semantics
- E2E layer for communication traffic
