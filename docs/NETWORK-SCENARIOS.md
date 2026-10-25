# Network scenarios

These are the Toxiproxy commands for the four network scenarios in Part 5 of the brief. Use them as written, so every submission is tested under the same conditions.

They were tested with Toxiproxy 2.12.0 (`ghcr.io/shopify/toxiproxy:2.12.0`).

## Before you start

Each instance connects to the tactical server through its own Toxiproxy proxy. In the commands below, `<proxy>` is the name of that proxy, for example `team-a`. To affect more than one instance, run the commands for each proxy.

Creating a proxy looks like this:

```sh
toxiproxy-cli create --listen 0.0.0.0:<listen-port> --upstream <tactical-server>:<port> <proxy>
```

`toxiproxy-cli` talks to the Toxiproxy API at `http://localhost:8474`. Use `TOXIPROXY_URL`, or the `--host` flag before the command, to point it elsewhere. If Toxiproxy runs in a container, `docker exec <container> /toxiproxy-cli ...` works too.

Toxiproxy calls the direction from the instance to the tactical server **upstream**, and the way back **downstream**. Telemetry flows upstream. A toxic added without `--upstream` only affects the way back.

Each scenario also lists the HTTP API calls, for use in scripts.

## 1. Short outage: down for 30 seconds

```sh
toxiproxy-cli toggle <proxy>    # cut
sleep 30
toxiproxy-cli toggle <proxy>    # restore
```

`toggle` flips the current state. Check the state with `toxiproxy-cli list` if you are unsure. The API sets it explicitly:

```sh
curl -s -X POST http://localhost:8474/proxies/<proxy> -d '{"enabled": false}'
sleep 30
curl -s -X POST http://localhost:8474/proxies/<proxy> -d '{"enabled": true}'
```

What happens: Toxiproxy closes the open connections at once and refuses new ones until the proxy is enabled again.

## 2. Long outage: down for 10 minutes

The same commands as the short outage, with `sleep 600`.

## 3. High latency: 2,000 ms with 500 ms jitter, for 5 minutes

The delay applies in both directions, so a round trip takes about 4 seconds.

```sh
toxiproxy-cli toxic add -t latency -n latency_up --upstream -a latency=2000 -a jitter=500 <proxy>
toxiproxy-cli toxic add -t latency -n latency_down --downstream -a latency=2000 -a jitter=500 <proxy>
sleep 300
toxiproxy-cli toxic remove -n latency_up <proxy>
toxiproxy-cli toxic remove -n latency_down <proxy>
```

API:

```sh
curl -s -X POST http://localhost:8474/proxies/<proxy>/toxics \
  -d '{"name":"latency_up","type":"latency","stream":"upstream","attributes":{"latency":2000,"jitter":500}}'
curl -s -X POST http://localhost:8474/proxies/<proxy>/toxics \
  -d '{"name":"latency_down","type":"latency","stream":"downstream","attributes":{"latency":2000,"jitter":500}}'
sleep 300
curl -s -X DELETE http://localhost:8474/proxies/<proxy>/toxics/latency_up
curl -s -X DELETE http://localhost:8474/proxies/<proxy>/toxics/latency_down
```

What happens: each chunk of data is held for 1.5 to 2.5 seconds. Toxiproxy keeps the order, so a chunk never overtakes the one before it, and most delays end up close to 2.5 seconds. When the toxics are removed, data that is still held is passed on straight away.

## 4. Low bandwidth: 10 KB/s for 5 minutes

The limit applies from the instance to the tactical server.

```sh
toxiproxy-cli toxic add -t bandwidth -n bandwidth_up --upstream -a rate=10 <proxy>
sleep 300
toxiproxy-cli toxic remove -n bandwidth_up <proxy>
```

API:

```sh
curl -s -X POST http://localhost:8474/proxies/<proxy>/toxics \
  -d '{"name":"bandwidth_up","type":"bandwidth","stream":"upstream","attributes":{"rate":10}}'
sleep 300
curl -s -X DELETE http://localhost:8474/proxies/<proxy>/toxics/bandwidth_up
```

What happens: Toxiproxy counts 1 KB as 1000 bytes, so about 10,000 bytes per second get through. Data above that rate waits in Toxiproxy and in the socket buffers, so a sender can keep writing for minutes before a write blocks. When the toxic is removed, the queued data arrives at once.
