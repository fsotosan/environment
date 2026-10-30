# Calibration and placement

The station is designed for **trends and relative comparisons**, not regulatory
measurement. This page explains what can be calibrated, what cannot, and how to
avoid the most common mistakes.

## Before calibrating

- Let the station run for at least 30 minutes after power-up. The SEN5x fan and
  the enclosure need to reach thermal equilibrium.
- Confirm the timezone and clock. Daily aggregates use the station timezone; the
  CM5 has no battery-backed RTC unless the CR2032 cell is fitted.
- Run `envmon doctor` and fix every critical check first.
- Record a baseline in the dashboard before changing settings, so you can compare.

Settings live in `/etc/envmon/config.yaml` (native) or
`config/config.yaml` (Docker/dev). Restart the service after changing hardware
offsets:

```bash
sudo systemctl restart envmon
```

Environment variables override YAML, so check `systemctl cat envmon` and
`/etc/envmon/envmon.env` for stale `ENVMON_*` values.

---

## Noise (approximate LAeq)

### What the hardware actually does

The Grove Analog Microphone outputs a biased analog signal. The Grove Base HAT
ADC is a **slow I²C-polled 12-bit ADC**, not an audio codec:

- effective rate is typically **1–2 kHz**, occasionally lower on a slow bus;
- there is no characterised anti-aliasing filter;
- the microphone's frequency response and sensitivity vary between modules;
- the ADC's reference voltage and gain error are not individually calibrated.

The software computes an A-weighted RMS level from a short FFT, reports it as
`noise_db_a`, and stores the measured sample rate and clipping percentage with every
sample. This is useful for "quieter/louder than yesterday" questions. It is **not**
a Type 1/Type 2 sound-level meter and must not be used for workplace safety,
hearing-conservation or legal compliance.

### What you need

- A reference sound source with a known level, ideally a **94 dB / 1 kHz acoustic
  calibrator** fitted to the microphone.
- Failing that, a phone SPL meter or a commercial sound-level meter placed next to
  the Grove microphone. Phone meters are themselves approximate; use the same
  phone and app for before/after comparisons.
- A quiet room for the baseline and a louder, steady source for the check.

### Procedure

1. Set `sensors.noise.calibration_offset_db: 0.0` and restart.
2. Place the reference source at the same distance/orientation as normal office
   sound. Avoid wind, HVAC outlets and reflective corners.
3. Read the current value from the dashboard or:
   ```bash
   curl -s http://localhost:8080/api/current | jq '.sample | {noise_db_a, noise_peak_db, noise_dbfs, noise_clipping_pct, noise_sample_rate_hz}'
   ```
4. Measure the reference level with the meter/calibrator at the same position.
5. Set the offset to `reference_db - measured_db_a`. For example, if the calibrator
   says 94.0 dB and the station reports 81.3 dB, set `+12.7`.
6. Restart, wait for a few samples, and repeat the comparison. One adjustment is
   usually enough; module-to-module differences are larger than the residual.
7. Check a second, louder level to confirm linearity. If the error changes a lot,
   the microphone or ADC is clipping or the source is too close.

```yaml
sensors:
  noise:
    enabled: true
    adc_address: 0x04        # 0x08 on some Base HAT revisions
    adc_channel: 0           # Grove A0
    sample_rate_hz: 2000
    duration_seconds: 1.0
    sensitivity_mv_per_pa: 10.0
    calibration_offset_db: 12.7
    a_weighting: true
    highpass_hz: 20.0
    adc_reference_volts: 3.3
```

### Troubleshooting

| Symptom | Action |
|---|---|
| `ADC sampled at only … Hz` | Use a short, good-quality Grove cable; check the I²C bus runs at 100 kHz; avoid sharing the bus with long stubs |
| `noise_clipping_pct` above a few percent | Move the microphone farther from the source, reduce playback level, or add attenuation; the ADC cannot be gain-adjusted in software |
| Values pinned near 0 dB | Check the mic is on A0, the ADC address (`0x04` vs `0x08`) is correct, and the Grove cable is seated |
| Values drift with temperature | Expected for an uncalibrated electret; re-check the offset seasonally |
| Values much lower than a phone meter | Phone weighting/app differences; calibrate against a real meter or calibrator, not a phone alone |
| High-pass errors at startup | Lower `highpass_hz` or raise `sample_rate_hz`; the configured high-pass must stay below Nyquist |

### Interpretation

- `noise_db_a` is an A-weighted, short-term estimate. It is not an energy-averaged
  LAeq over a full workday.
- The simulator is not calibrated either; it exists to exercise the UI.
- Keep the `noise.approximate: true` field and the dashboard wording. Do not build
  alerting that claims a compliance threshold.

---

## Temperature and humidity (SEN5x)

The SEN5x is factory-calibrated, but the enclosure and the Pi itself affect the
reading:

- Self-heating from the CM5 and the SEN5x fan can raise the measured temperature.
- Direct sunlight, radiators, monitors and ceiling lights cause local bias.
- Humidity is relative to the sensor's local temperature.

Placement rules:

- Mount the sensor away from the Pi's heat plume, at breathing height (about
  1.1–1.5 m), with free air movement.
- Avoid direct sun, air-conditioner outlets and radiators.
- Do not enclose the sensor in a sealed box; the SEN5x needs airflow.

If the station consistently reads high by a stable amount compared with a
reference thermometer after 30+ minutes of equilibrium, compensate:

```yaml
sensors:
  sen5x:
    temperature_offset_c: -0.8   # example only; measure your own offset
```

Use a small offset (typically within ±2 °C). A large offset usually means a
placement problem, not a sensor problem. Humidity has no software offset; fix
placement or accept the reading.

The SEN5x performs automatic fan cleaning periodically. A brief "no new data"
error during cleaning is normal and the sampler retries.

VOC and NOx values are **indices**, not concentrations:

- VOC index is relative to Sensirion's gas-tuning baseline; 100 is the typical
  baseline, higher means more reducing gases.
- NOx index is only present on the SEN55; it behaves similarly for oxidizing gases.
- Neither index converts to ppm or µg/m³. Use them for trends, not exposure limits.
- The default thresholds (250) are dashboard attention hints, not health limits.

PM values are mass concentrations from the SEN5x and are the most directly useful
air-quality numbers. The default attention levels (25 µg/m³ PM2.5, 50 µg/m³ PM10)
are configurable hints, not regulatory limits.

---

## Light (TSL2561)

The TSL2561 has two channels (visible+IR and IR). The driver applies the
datasheet's piecewise lux formula after normalising for gain and integration time.

- `sensors.light.gain: 16` is best for typical indoor light. If readings sit at
  the top of the range or look saturated in bright daylight, set `gain: 1`.
- `integration_time_ms` can be `14`, `101` or `402`. `402` gives the best
  resolution in dim rooms; `14` reduces motion/response time in bright rooms.
- The sensor is directional. Point it at the surface or area of interest, not at
  the ceiling, and avoid shadows from the enclosure.
- Lux is a rough illuminance estimate, not a photometric calibration. For
  workplace lighting surveys use a calibrated lux meter.
- IR-rich sources (sunlight, some halogen lamps) can confuse the ratio-based
  formula. The driver clamps negative/invalid results to zero and returns zero
  when the IR channel dominates.

There is no software gain calibration; use the gain/integration settings and fix
placement.

---

## Time and daily aggregates

- Raw timestamps are UTC epoch seconds. Daily statistics use `timezone`.
- Without the CM5 RTC battery, a power cut can reset the clock until NTP syncs.
  Samples recorded before NTP correction may land on the wrong local day.
- After changing `timezone`, recompute recent history:
  ```bash
  envmon aggregate --days 30
  ```
  Existing `daily_stats` rows are replaced per day; raw samples are unchanged.
- After a long outage, the maintenance scheduler aggregates up to seven missing
  days on startup. Use `envmon aggregate --days N` for a larger repair.

---

## Verification checklist

After any calibration or hardware change:

1. `envmon doctor` — all critical checks pass; sensors report `ok`.
2. `envmon sample-once --json` — plausible values, no unexpected `errors`.
3. Dashboard — simulator banner absent in hardware mode; status is `Live`, not
   `Degraded`.
4. `/api/status` — `sample_age_seconds` is small, `consecutive_failures` is 0.
5. Compare temperature/humidity/noise/lux against independent references at the
   same location and record the offsets in your site notes.
6. `envmon backup` — a fresh backup exists before you change hardware again.

Keep a short site log: sensor revision, ADC address, cable lengths, offsets,
reference instrument, date and who calibrated. Future-you will need it.
