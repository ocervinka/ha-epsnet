# ha-epsnet

English · [Česky](README.cs.md)

Home Assistant custom integration for Tecomat Foxtrot / iNELS PLCs, talking to the PLC
directly over EPSNET UDP — no PLCComS gateway needed.

Not affiliated with or endorsed by Teco a.s.; Tecomat, Foxtrot and iNELS are their trademarks.
The EPSNET support here was written for interoperability and is tested on one installation
(an iNELS 2 CU2-01M central unit — first-generation Foxtrot hardware — with the modules listed below,
Home Assistant 2026.2 and later). Other Foxtrot central units are untested.
Reports from other setups are welcome: open the integration, choose **⋮ → Download diagnostics** and
attach the file to an issue. It contains your PLC variable names; rename them if you prefer.

Modules the integration doesn't know yet are not created as devices; they are listed in a
**Repairs** notice instead (with their type codes), so you know what is missing.

## Current capabilities

- direct EPSNET UDP transport; PLCComS is not required
- live discovery of the installed CIB modules during setup; each device from the variable map is
  paired with its physical module by the module's position in the PLC program
- the published-variable map (the program's `.pub`) read straight from the PLC, or uploaded
- without a map: every input and output of the supported CIB modules, read from the PLC program's
  own I/O configuration and shown read-only
- Home Assistant entities for lights, switches, binary sensors, and sensors
- grouped/block polling by default, with automatic fallback to one-by-one reads on grouped-read errors
- diagnostic merged-catalog output plus poll timing sensors

## Supported modules

The integration currently knows the data layout of:

- `SA2-04M`
- `SA2-012M`
- `DA2-22M`
- `WSB2-20`
- `WSB2-40`
- `IM2-40B`
- `IM2-140M`
- `C-IT-0200I`
- `C-RC-0011R` (temperature, humidity, external temperature, buttons; laid out from its device
  description, not yet tested on a real installation)

## Installation

### Marketplace (Home Assistant 2026.11 and later)

1. **Settings → Marketplace → Add from link**, enter `ocervinka/ha-epsnet` and select **Add**.
2. Install **EPSNET**.
3. Restart Home Assistant.

### HACS (Home Assistant before 2026.11)

1. Add `https://github.com/ocervinka/ha-epsnet` as a custom repository in HACS with category
   **Integration**.
2. Install **EPSNET** from HACS.
3. Restart Home Assistant.

### Manual

Copy `custom_components/epsnet` into your Home Assistant config directory under
`custom_components/epsnet`, then restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → EPSNET**, then:

1. **Connection** — a name, the PLC host or IP, the EPSNET UDP port (default `61682`), the local
   UDP source port (default `61681`) and the scan interval (milliseconds of pause after each poll;
   default `500`, `0` = immediately).
2. **Variable map** — either **discover it from the PLC** (recommended: the published-variable map
   is read straight out of the running program) or **upload an exported `.pub` / `.piz` / `.zip`**.
3. **Confirm** — shows the PLC identity, the number of modules found and the number of published
   points.

Either way the PLC's module inventory is also scanned live, so entities are created only for
hardware that is actually there. The map is stored in the config entry and is not re-read on its
own: after changing the PLC program, use **Configure → Re-import the variable map from the PLC**,
or **… from a file (.pub)** when the PLC holds no map or an old one (a program loaded with
Mosaic). Either way the entry and its entity IDs stay; a repair notice tells you when the map
actually changed, and a file that pairs with none of the PLC's modules is refused.

### The variable map

The map is the program's `.pub` file: the list of published variables with their names and
addresses in the PLC. It gives entities your own names and is what the integration switches
through (the program's `_ON` / `_OFF` command variables).

Without a map, setup can still continue: the integration reads the I/O configuration compiled into
the PLC program (which module sits where in the PLC's memory) and creates an entity for every input
and output of the supported CIB modules — buttons, inputs, temperatures, relay and LED states,
dimmer levels. They are **read-only** and named by module type and bus address (e.g. *WSB2-40 1A2B
Temperature*): the PLC program rewrites the outputs every cycle, so they can be shown but not
switched without the program's command variables. Adding the `.pub` later keeps those entities
(same entity IDs, history and automations), gives them the names from your program and adds the
lights and switches you can control. Modules on a CPU's internal bus (not CIB) are not covered.

With a map, the channels none of its entities shows (e.g. tamper contacts, status flags, the
relay modules' local switches, the physical output states) are added as well, disabled — enable
the ones you want. Disabled entities are not read from the PLC.

When the PLC gets a new program, the integration notices (the program checksum changes), waits
until the programming tool is done, and reads the I/O configuration again; if it changed, the
integration reloads. Meanwhile the entities placed from the program are unavailable.

- **FoxTool** stores the `.pub` in the PLC when it loads the program, so *discover from the
  PLC* finds it.
- **Mosaic** writes it to the project folder on your PC, and only with *Project manager → Sw →
  Export files → Assembler (.pub)* enabled. Upload that file.

Discovery and the map fetch use a temporary UDP port, so FoxTool, Mosaic and the Firmware Updater
can connect to the PLC while Home Assistant is running (the PLC's single management session is taken only
during the few minutes of a map fetch).

**Configure** on the integration later offers *Re-import the variable map from the PLC*,
*Re-import the variable map from a file (.pub)* and the polling intervals: the pause between full
reads of all values (default 500 ms) and the fast lane below. *Reconfigure* changes host and ports.

## Languages

The setup screens, options, repair notices and diagnostic sensor names are translated into
**English**, **Czech** and **Slovak**; Home Assistant picks the language from your user profile. Entity names
for lights, switches and inputs come from the variable names in your PLC program, so they appear
exactly as you named them there.

Translations live in `custom_components/epsnet/translations/`; `strings.json` is the English
source and a test checks every translation has the same keys and placeholders. To add a
language, copy `en.json` to `<language code>.json` and translate the values.

## Diagnostics and performance

**Download diagnostics** (on the integration's ⋮ menu) returns the merged catalog: discovered
modules and their type codes, how they matched the published variables, unsupported modules, the
raw discovery reads, the full stored variable map (one line per variable, as in the `.pub`), the
program checksum the PLC reports, the I/O configuration read from the program (with the raw bytes)
and, with a map, a check of every map variable against it. To post the file publicly, turn on *Configure → Polling intervals and diagnostics →
Hide variable names in diagnostics*: your own words in the names become placeholders (`w1`, `w2`…),
consistently, so the structure stays readable. The same catalog (without the raw reads) is also written at startup to
`epsnet/<entry_id>_merged_catalog.yaml` in the Home Assistant config directory.

The integration also exposes diagnostic timing sensors for:

- average read duration
- minimum read duration
- maximum read duration
- average cycle duration
- minimum cycle duration
- maximum cycle duration
- average / minimum / maximum block read duration, and the number of read blocks

The averages are exponential moving averages across polls. The minimum and maximum values cover the
raw samples seen since the previous metric publish window. These timing metrics publish on the first
successful poll and then no more than about every five seconds.

Grouped/block polling is the default production read path.

## Current behavior and limitations

- The direct EPSNET backend is reverse-engineered and intentionally focused on the request/response
  patterns needed by the current entity set.
- Without a variable map, inputs and outputs are read-only and CIB modules only (see above).
- Advanced room-controller features such as richer HVAC/setpoint behavior are still out of scope for the
  current public release.

## Testing

The suite is layered so most of the surface is covered without any PLC, and each
tier has a clear job:

1. **Pure unit** — `.pub` parsing, catalog building / device matching, protocol
   codec (frame build/parse/split, the `6c 23` size-header parse), map
   serialization/CRC, migration, and timing-metric math. No I/O.
2. **Client integration** — the real `EpsnetUdpClient` against an in-process
   **fake UDP server** (`tests/test_epsnet_udp.py`): command/response framing,
   sequencing, grouped polling + one-by-one fallback, and the batched on-PLC
   fetch. Real localhost sockets.
3. **In-process Home Assistant** — `tests/test_integration.py` via
   [`pytest-homeassistant-custom-component`](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component):
   the real config flow → entry → entities → coordinator → services run inside a
   real `HomeAssistant`, with the client patched to a small synthetic dataset (no
   PLC, no sockets). This is the HA-idiomatic integration test.

All three run in CI (`.github/workflows/tests.yml`), next to Home Assistant's
`hassfest` manifest check and the HACS validation (`.github/workflows/validate.yml`).

Run them:

```bash
# needs Python 3.13 (Home Assistant 2026.x) and the dev dependency:
pip install pytest-homeassistant-custom-component==0.13.316
pytest -q
```

> **Full-stack / real-PLC testing** (a Docker Home Assistant pointed at a recorded
> and replayed PLC, or a real one) is done separately: it needs a real PLC endpoint
> and captured traffic, neither of which belongs in this repository.

## Safety

- Treat live PLCs and Home Assistant instances as production unless you know otherwise.
- Do not commit private PLC exports, credentials, site-specific addresses, or captured traffic with
  sensitive data.
- Keep reverse-engineering material, private PLC exports, credentials, and captured traffic with
  sensitive data out of this repository.

## Fast lane for binary inputs (buttons, PIRs)

A full poll reads every state in grouped spans (≈0.6 s for ~250 states on a CU2-01M over
Wi-Fi/LAN), so with the default 500 ms scan interval each input is sampled only every ~1.1 s
and a wall-button press shorter than that can be missed entirely. **Options → Change the
polling intervals → Fast lane** polls just the `binary_sensor` inputs on their own interval
(100 ms recommended; 0 = off). The transport lock is held per span, so fast reads and writes
interleave with the full cycle instead of queueing behind it; once the fast lane runs, the
full cycle stops re-reading those inputs (shorter cycle, and a fresh fast value is never
overwritten by an older full-cycle one). Cost: ~10 small UDP exchanges per second.

## See also

Not on Home Assistant, or want arbitrary PLC variables on MQTT for older devices which do not
support MQTT? [plccoms-mqtt-bridge](https://github.com/ocervinka/plccoms-mqtt-bridge) bridges
Teco's PLCComS to an MQTT broker (openHAB, Node-RED, Home Assistant MQTT).

## License

[MIT](LICENSE)
