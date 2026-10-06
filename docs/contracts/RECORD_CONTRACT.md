# Record Contract

Status: **DRAFT specification (revision 2, corrected after verification), not approved.** No implementation exists.

Source tags: **[CO]** = `docs/cropsync_coding_overview.docx`; **[RB]** = `docs/CROPSYNC_Rover_Build_and_Parts.docx`; **[HSI]** = `docs/HARDWARE_SOFTWARE_INTERFACE.md` (an audit derived from CO/RB, not an independent source).

Classification used in every section:
- **SOURCE-ESTABLISHED**: directly stated in CO or RB.
- **PROPOSED DESIGN**: a suggestion, not required by the sources.
- **OPEN / TBD**: not defined by the sources; must be decided before implementation. IDs are `R-n`.

Companion contracts: `WEBSOCKET_CONTRACT.md` (IDs `W-n`), `ARM_SERIAL_CONTRACT.md` (IDs `A-n`).

## Shared Terminology (identical in all three contracts)

These terms are kept separate on purpose. **The sources do not define any relationship between them** unless a row says so.

| Term | What the sources say | Relationship to the others |
|---|---|---|
| Record sequence number | "sequence number" is one of the record items [CO s1]. | OPEN (R-3, R-4) |
| Rover ID | "rover ID" is one of the record items [CO s1]. | Part of "rover ID + counter" below |
| Retry-safe unique ID | "Unique IDs (rover ID + counter) make retries safe" [CO s4]. The sentence sits in the app paragraph about pulling and uploading records and photos. It doesn't say which objects carry these IDs, nor what the counter is. | Not stated to be the record sequence number or the photo ID (R-4) |
| ARM line sequence number | Every arm-cable line carries "a sequence number and checksum" [CO s1]. | Not stated to relate to the record sequence number (A-3) |
| Drive command sequence number | None in the sources. Listed only as a possible future item. | Does not exist in the sources (W-7) |
| Photo ID | `photo-id` field of `LEAF`, and a record item [CO s1]. | Not stated to relate to any of the above (R-12) |
| Received / queued / uploaded | The sources use "phone confirms upload" [CO s2] and "uploads when mobile data is available" [CO s4]. They don't define "received" or "queued" as confirmation events. | OPEN (W-11) |

## Purpose

SOURCE-ESTABLISHED: At each stop the rover reads soil, air and battery, and has the camera board check a leaf [RB]. Results are stored on the rover [RB], in LittleFS until the phone confirms upload [CO s2]. The phone app pulls records and photos over the rover's Wi-Fi, queues them in IndexedDB, and uploads them when mobile data is available [CO s4]. The record is one of the three shared contracts that "hold everything together" [CO s1], and "record format" is covered by unit tests [CO s9].

## Source-Established Requirements

1. The record is **JSON** [CO s1].
2. It carries these items [CO s1]: time, sequence number, rover ID, battery, soil, air, sensor status flags, leaf label, confidence, photo ID, and the firmware, model, rules and calibration versions.
3. Each sensor has a status flag, so one failure never blocks a record [CO s2].
4. Records are stored in LittleFS until the phone confirms upload [CO s2].
5. "Unique IDs (rover ID + counter) make retries safe" [CO s4]. Which objects carry them is not stated (see Terminology, R-4).
6. `LEAF <class> <conf> <photo-id>` is the camera board's result line [CO s1]. The camera returns "the majority label" of 3 photos, and reports "unclear" if the top score is under 70% or is not at least 20 points ahead of the second [CO s3].
7. NPK probe values pass through the calibration correction before the rules see them [CO s6]. The correction per nutrient is `Lab = a*Probe + c*Moisture + b`, with a, b and c in a config file the app can update. The unit conversion is `kg/ha = mg/kg x 2.24` [CO s8].
8. Sensors that exist on the rover: NPK probe (JXBS-3001), capacitive soil moisture, DS18B20 soil temperature, BME280 (air temperature, humidity, pressure), BH1750 (daylight), battery voltage divider [RB]. Rev 7 adds a second DS18B20 on the battery: "Battery temperature, shown as an alert in the app" [RB].
9. Versions: rules live in "a versioned data file" [CO s6]; a, b, c live in a config file the app can update [CO s8]. The record must carry firmware, model, rules and calibration versions [CO s1].
10. The app's Replay mode is "clearly labelled 'recorded data'" [CO s4]. The mock rover "returns fake readings and a sample photo" and has an "unclear" switch [CO s9].
11. The proxy's `/second-opinion` "sends the photo, readings, crop and language" [CO s5]. The sources do not say that the record is the proxy's request format.

## Field Definitions

SOURCE-ESTABLISHED: the item names. Everything else in the right-hand columns is interpretation or open.

| Item (source) | Proposed interpretation (NOT source) | Open |
|---|---|---|
| time | When the record or test was made | R-2 |
| sequence number | An integer counter | R-3, R-4 |
| rover ID | Identifies the rover | R-5 |
| battery | Battery voltage (the source uses volts for the 9.9 V cut-off) | R-13 |
| soil | NPK, moisture, soil temperature (the sensors that exist) | R-13 |
| air | BME280 values; BH1750 may or may not belong here | R-13 |
| sensor status flags | One flag per sensor (the source says "each sensor has a status flag") | R-7 |
| leaf label | A label string; "unclear" is one possible value | R-10, R-11, R-17 |
| confidence | The `conf` of the `LEAF` line | R-9 |
| photo ID | The `photo-id` of the `LEAF` line | R-12 |
| firmware / model / rules / calibration versions | Provenance strings | R-6, R-18 |

## Unresolved issues stated explicitly

- **Versions (R-6).** The record requires firmware, model, rules and calibration versions [CO s1]. The rules and calibration are managed on the app side [CO s4, s6, s8]. The sources do not say how the rover obtains the rules and calibration versions. No synchronisation mechanism is defined, and none is assumed here. The source of the model version (camera board) and of the firmware version is also unspecified.
- **Time (R-2).** The record requires time [CO s1]. The sources do not define the authoritative time source or any synchronisation mechanism. Neither source names a time source. No mechanism is assumed.
- **Upload (R-15, W-11).** See `WEBSOCKET_CONTRACT.md` section on upload semantics.

## Validation

SOURCE-ESTABLISHED:
- The listed items are required [CO s1].
- A record must still be producible when an individual sensor fails [CO s2].
- The unclear rule applies to the leaf result [CO s3].
- NPK values used by rules are calibration-corrected [CO s6].

OPEN: numeric ranges, string lengths, nullability, maximum record size, schema versioning. (R-1)

## Error / Failure Behavior

SOURCE-ESTABLISHED: a sensor failure doesn't block a record, and a status flag exists per sensor [CO s2]. A record stays in LittleFS until the phone confirms upload [CO s2].

OPEN: R-8 (camera or leaf-check failure; the sources don't say it's covered by the per-sensor rule, and don't call the camera a sensor), R-16 (storage full).

## Proposed Design Decisions

PROPOSED, not source requirements:

- P1. The record is a single JSON object at the top level.
- P2. Include a `schema` version field so contract changes are detectable (the source's four version items don't cover this).
- P3. Mark records from the mock rover or Replay mode so they can't be mistaken for real data. The source requires the "recorded data" label in the app only [CO s4].
- P4. Rules and the app check the matching status flag before using a reading.
- P5. Keep `confidence` present when the label is "unclear".

## Open Decisions / TBD

| ID | Decision |
|---|---|
| R-1 | Field names, nesting, JSON types, units for every item; validation ranges |
| R-2 | **Time**: authoritative time source, synchronisation mechanism, format |
| R-3 | **Record sequence number** exact semantics: scope (per rover?), width, start, behaviour across reboot |
| R-4 | Whether the retry-safe unique ID (rover ID + counter) is built from the record sequence number, a separate counter, or applies to photos too |
| R-5 | Rover ID format and how it is assigned |
| R-6 | **Version provenance**: how rules and calibration versions reach the rover; where model and firmware versions come from; no mechanism is assumed |
| R-7 | Status-flag set and encoding; representation of a missing/failed reading; whether the camera or leaf check has a flag |
| R-8 | Effect of camera or leaf-check failure on label, confidence, photo ID, and on record creation |
| R-9 | **Confidence** meaning and scale (the thresholds in [CO s3] are given as 70% and 20 points; the `conf` value's scale is not defined) |
| R-10 | **Scope of "unclear"**: per photo, aggregate score, majority result, or other |
| R-11 | What happens when the 3 photos give different labels |
| R-12 | Photo ID format; which of the 3 photos it names; where photos are stored |
| R-13 | Which readings are included (BH1750 light, battery temperature); raw vs calibrated NPK; units (mg/kg vs kg/ha) |
| R-14 | Relationship between the record and the proxy's "photo, readings, crop and language" input; where crop and language come from (neither is a record item) |
| R-15 | What "phone confirms upload" means (see W-11) |
| R-16 | Behaviour when LittleFS storage is full |
| R-17 | Leaf class list |
| R-18 | Version string formats; how mock or replay data is marked (proposal P3) |

## Illustrative example (NOT an approved schema)

Field names and values are placeholders. This example is not a protocol requirement.

```json
{
  "time": "<TBD R-2>",
  "seq": "<TBD R-3>",
  "rover_id": "<TBD R-5>",
  "battery": "<TBD>",
  "soil": "<TBD>",
  "air": "<TBD>",
  "status": "<TBD R-7>",
  "leaf_label": "<TBD>",
  "confidence": "<TBD R-9>",
  "photo_id": "<TBD R-12>",
  "versions": { "firmware": "<TBD>", "model": "<TBD>", "rules": "<TBD R-6>", "calibration": "<TBD R-6>" }
}
```

## Source Traceability

| Item | Source |
|---|---|
| JSON, item list | CO s1 |
| Status flags, record never blocked, LittleFS | CO s2 |
| Unclear rule, 3 photos, majority label | CO s3 |
| Unique IDs, IndexedDB, upload when mobile data is available, Replay label | CO s4 |
| Proxy payload (photo, readings, crop, language) | CO s5 |
| Versioned rules file, NPK calibration order | CO s6 |
| a, b, c config, unit conversion | CO s8 |
| Mock rover, record-format tests | CO s9 |
| Sensors, Rev 7 battery temperature, per-stop flow | RB |
| Known gaps | HSI section J |
