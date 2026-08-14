# LLazarus

LLazarus is a small OpenAI-compatible proxy that uses Wake-on-LAN to wake
sleeping inference devices on demand. It discovers models from multiple
OpenAI-compatible endpoints, keeps their routes in SQLite, and streams backend
responses without buffering them.

It has no UI, authentication, scheduler, model manager, or automatic sleep logic.

### Why?

Letting all inference satellites run permanently not only consumes heaps of electricity,
but may also reduce their lifespan. Letting satellites suspend especially makes
sense for rarely-used inference satellites. Instead of manually waking the satellite,
LLazarus does that seamlessly by being the middle man between the AI frontend and the model.

### Why the name?

“LL” refers to LLMs, while “Lazarus” reflects bringing sleeping inference devices back to life with Wake-on-LAN.

### Which installation methods exist?

Curently only TrueNAS SCALE deployment is tested as that is what I am running.

## TrueNAS SCALE deployment

The image is built on a separate PC and imported into TrueNAS. No Compose file
or source checkout is needed on TrueNAS.

On the build PC, from this project directory:

```sh
docker build --platform linux/amd64 -t llazarus:latest .
docker save -o llazarus.tar llazarus:latest
```

Copy `llazarus.tar` to TrueNAS using `scp`, the TrueNAS shell, or another file
transfer method. On TrueNAS, import the image from the command line:

```sh
docker load -i /path/to/llazarus.tar
docker image ls llazarus
```

This makes `llazarus:latest` available locally. Create a custom app/container
using that image with these settings:

- Name: `llazarus`
- Repository: `llazarus`
- Network: host mode
- Host path `/mnt/POOL/apps/llazarus` (or wherever you store application data) mounted to container path `/data`

Create the host path before starting the app. LLazarus creates `config.yml` and
`router.db` in that directory automatically; restart the app after editing the
configuration. The mounted directory must be writable by the container.

After starting, inspect the app
logs in the TrueNAS UI and check the API at:

```text
http://TRUENAS-IP:4000/v1/models
```

## Configuration

On first startup, LLazarus copies `config.example.yml` to `config.yml` in the persistent
application directory if one does not already exist. Edit that file to add your
devices, MAC addresses, and endpoints. `config.example.yml` is the source template
configuration shape for reference. Each endpoint is the OpenAI-compatible base
URL ending in `/v1`.

```yaml
server:
  port: 4000
  # Maximum time for one ICMP ping attempt, in seconds.
  ping_timeout: 1
  # Delay between ping attempts while waiting for a device to wake, in seconds.
  ping_interval: 0.5
  # Maximum total time to wait for a sleeping device to respond after WoL, in seconds.
  wake_timeout: 30
  # Maximum time to wait for the inference endpoint after the device responds to ping, in seconds.
  service_timeout: 60
  # Maximum time to establish a TCP connection to an inference endpoint, in seconds.
  connect_timeout: 2
  # Maximum time without receiving response data from the backend, in seconds.
  read_timeout: 1800
  # Maximum time allowed while sending a request to the backend, in seconds.
  write_timeout: 30

devices:
  aisatellite1:
    ping: "192.168.178.67"
    mac: "AA:BB:CC:DD:EE:FF"
    endpoints:
      - "http://aisatellite1:8080/v1"

  gpu-server:
    ping: "192.168.178.100"
    mac: "11:22:33:44:55:66"
    endpoints:
      - "http://gpu-server:8000/v1"
      - "http://gpu-server:8001/v1"

  always-on:
    endpoints:
      - "http://server:8000/v1"
```

`ping` and `mac` are optional. When `ping` is configured, it represents physical
device state and `/models` represents inference-service state. If ping succeeds
but the endpoint is unavailable, the router waits for the service and does not
send WoL again. Without `ping`, endpoint reachability is used as the wake signal.

Model IDs form one global namespace because `model_id` is the SQLite primary key.
Give models unique IDs across endpoints. If multiple reachable endpoints report
the same ID, the last endpoint in YAML order owns that route after discovery.

## API

Configure AnythingLLM, Open WebUI, Hermes, or another OpenAI client with this base
URL only:

```text
http://TRUENAS-IP:4000/v1
```

The router exposes cached models without waking any device:

```sh
curl http://TRUENAS-IP:4000/v1/models
```

All `POST /v1/*` requests with a top-level `model` field are routed generically,
including chat completions, completions, embeddings, and responses. Backend HTTP
statuses and headers are passed through, and response bodies are streamed as they
arrive, including `text/event-stream` responses.

## Cache behavior

At startup the YAML configuration first removes cached rows for deleted devices
and deleted endpoints. Every endpoint that successfully returns `/models` is then
synchronized exactly: new models are inserted and models no longer reported by
that endpoint are deleted. Rows belonging to an unreachable endpoint are retained
so a request can still identify and wake its sleeping device.

The service reads routes into memory after synchronization. Restart the router
after changing `config.yml` or adding/removing models on a backend.

The router does not implement client authentication. Run it on a trusted network
or restrict access to port 4000 with the TrueNAS host firewall.

## Local development

Use any writable application-data directory containing `config.yml`:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
cp config.example.yml data/config.yml
APP_DATA="$PWD/data" python -m app.main
```

---

This project is licensed under MIT. ChatGPT and Codex have aided in the development process.
