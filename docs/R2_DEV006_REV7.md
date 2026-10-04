# R2 DEV-006 rev7 — Raw MAVLink evidence follows the declared UDP stream

The managed launch already publishes MAVLink to two explicit outputs:

- udp:127.0.0.1:14550
- udp:127.0.0.1:14555

The old verifier incorrectly treated TCP 5762 as the evidence channel. Rev7
uses the second declared output as a dedicated receive-only probe:

`udpin:127.0.0.1:14555`

TCP 5760/5762 remain useful only as stale-SITL preflight/cleanup guards.

The probe only waits for HEARTBEAT and sends no MAVLink messages or flight
commands.
