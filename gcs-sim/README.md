# Telemetry simulator

## What it is

`telemetry_sim.py` plays the ground control station (GCS) of one drone team. It pushes synthetic telemetry for the team's drones to the team's instance, which collects it.

The GCS and the instance are on the team's local network. This traffic does not cross the tunnel to the tactical server.

The positions are made up. The drones fly circles over open sea.

## Running it

It needs Python 3.10 or newer and has no dependencies.

```sh
python3 telemetry_sim.py --team-id team-a --target 127.0.0.1:4600
```

With Docker, sending to port 4600 on the host:

```sh
docker build -t telemetry-sim .
docker run --rm --add-host=host.docker.internal:host-gateway \
  -e SIM_TEAM_ID=team-a -e SIM_TARGET=host.docker.internal:4600 telemetry-sim
```

In Docker Compose, set `SIM_TARGET` to the agent's service name and port, for example `SIM_TARGET=agent:4600`.

To look at the stream, run `nc -klu 4600`. Messages print back to back because they have no trailing newline.

## Configuration

Each flag has an environment variable. The flag wins if both are set.

| Flag        | Env var       | Default          | Meaning                                                       |
| ----------- | ------------- | ---------------- | ------------------------------------------------------------- |
| `--team-id` | `SIM_TEAM_ID` | none, required   | Team id, used in `team_id` and `drone_id`.                    |
| `--drones`  | `SIM_DRONES`  | `2`              | Number of drones, at least 1.                                 |
| `--rate`    | `SIM_RATE_HZ` | `50`             | Messages per second per drone.                                |
| `--target`  | `SIM_TARGET`  | `127.0.0.1:4600` | `host:port` of the instance. The host is resolved every 10 s. |
| `--origin`  | `SIM_ORIGIN`  | `-10.0,-30.0`    | Centre of the drones' circles, `lat,lon` in degrees.          |

## Message format

Each message is one compact JSON object in UTF-8. Keys are always in this order:

```json
{"team_id":"team-a","drone_id":"team-a-1","seq":123456,"ts_ms":1792577730412,"lat":-10.1234567,"lon":-30.1234567,"alt_m":131.4,"heading_deg":287.4,"speed_mps":11.8,"battery_pct":61}
```

| Field         | Type   | Unit and meaning                                              |
| ------------- | ------ | ------------------------------------------------------------- |
| `team_id`     | string | Team id.                                                      |
| `drone_id`    | string | `<team_id>-<n>`, with `n` starting at 1.                      |
| `seq`         | int    | Message counter per drone, starting at 0.                     |
| `ts_ms`       | int    | Time the message was built, Unix time in milliseconds.        |
| `lat`         | float  | Latitude, WGS84 degrees, 7 decimals.                          |
| `lon`         | float  | Longitude, WGS84 degrees, 7 decimals.                         |
| `alt_m`       | float  | Altitude above mean sea level in metres, 1 decimal.           |
| `heading_deg` | float  | Direction of travel, degrees clockwise from north, 0 to 360.  |
| `speed_mps`   | float  | Ground speed in m/s, 1 decimal.                               |
| `battery_pct` | int    | Battery charge in percent, 0 to 100.                          |

## How an instance receives it

The GCS sends each message as one UDP datagram. A datagram holds exactly one JSON object of about 180 bytes, with no framing and no newline.

The GCS does not wait, retry or buffer. Anything the instance does not read is lost.

## Data rate

A message is about 180 bytes. With 2 drones at 50 Hz that is 100 messages/s, or about 18 KB/s per team.

## Notes

- `seq` restarts at 0 when the simulator restarts.
- `ts_ms` comes from the simulator's clock.
