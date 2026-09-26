# Effective Min Power Scaling

GlueScript.power_range min and max effectiveness depends upon speed. As speed decreases the effective min increases.

## Configuration variables

Three GlueScript configuration variables control the scaling:

- `power_floor` = The minimum power setting (percentage) at which the laser will fire. This defaults to 8%. This is used to bias the power range for calculation.
- `max_cut_speed` = The maximum cut speed (mm/s) supported by the hardware. This defaults to 400mm/s.
- `power_scaling_enabled` = Master switch for effective-min power scaling. Defaults to `True` (ON).

All three are per-instance configuration: they are never reset by `new_gluescript()` or the re-stage reset block (mirroring the `_warn_inline`/`_warn_comment_only` pattern).

## Algorithm

The algorithm added to the GlueScript class for adjusting the min power based upon power range and cut speed is:

```
def _effective_min_power(self, min_power, max_power, cut_speed):
    if self.max_cut_speed <= 0:
        raise ValueError("max_cut_speed must be > 0")
    max_cut_speed = self.max_cut_speed
    power_floor = self.power_floor
    eff_cut_speed = max(0.0, min(cut_speed, max_cut_speed))
    unbiased_min_power = max(min_power - power_floor, 0)
    unbiased_max_power = min(max_power, 100) - power_floor
    span = unbiased_max_power - unbiased_min_power
    return (unbiased_max_power - ((eff_cut_speed / max_cut_speed) * span)) + power_floor
```

Notes on the final design:

- The guard comes FIRST: a non-positive `max_cut_speed` raises `ValueError` before any calculation.
- Negative cut speeds clamp to 0 (the `max(0.0, ...)` term), so a negative speed behaves like zero speed — the effective min equals the maximum.
- The local variable is named `span` (not `power_range`) to avoid shadowing the `power_range()` method name.
- The `power_floor` biases the calculation so the laser's non-firing band is excluded from the ramp span.

### Worked table (min=10, max=70, floor=8, max_cut_speed=400)

| cut_speed | effective min | emitted range |
|-----------|---------------|---------------|
| 400       | 10.0          | [10, 70]      |
| 300       | 25.0          | [25, 70]      |
| 200       | 40.0          | [40, 70]      |
| 100       | 55.0          | [55, 70]      |
| 10        | 68.5          | [68.5, 70]    |
| 0         | 70.0          | [70, 70]      |
| 500       | 10.0          | [10, 70]      |
| -10       | 70.0          | [70, 70]      |

## Speed tracking

`power_range()` scales its emitted minimum from the current layer's cut speed:

- `declare_layer()` records its `speed` argument as the current layer's cut speed.
- `cut_speed()` overrides it.
- `move_speed()` does NOT update the tracked cut speed.

The tracked speed is per-job state: it is reset to 100.0 by `new_gluescript()` and the re-stage reset block.

## Deferred emission (flush at cut)

`power_range()` and `cut_speed()` no longer emit rpascript at call time.
They SAVE their settings as per-layer pending state and mark a dirty flag:

- `power_range()` saves `_pending_min_power`/`_pending_max_power` (the
  resolved values) and the pending `# warning:` comment lines, then sets
  `_power_dirty`.
- `cut_speed()` saves `_current_layer_speed` and sets `_speed_dirty`.

The next `cut_*` action (`cut_xy_to`/`cut_x_to`/`cut_y_to`) calls
`_flush_layer_settings()` at its top, BEFORE emitting the `CUT_*` line.
The flush emits ONLY what changed:

- `_speed_dirty` → `CUT_SPEED_LASER_1 Layer:{n} Speed={speed}` (cleared).
- `_power_dirty` → `LAYER_FREQUENCY`-if-changed (flag cleared),
  `SELECT_LAYER Layer:{n}`, `LAYER_MIN_POWER_1 Power:{emitted_min}%`,
  `LAYER_MAX_POWER_1 Power:{pending_max}%`, then the pending warnings
  (cleared).

`emitted_min` is computed at FLUSH time from the pending range and the
current layer speed: `min(_effective_min_power(pending_min, pending_max,
self._current_layer_speed), pending_max)` when `power_scaling_enabled`,
else `pending_min` unchanged.

Consequences of the strict flush-at-cut spec:

- **Dropped without a cut:** pending settings with no following cut are
  dropped at the `declare_layer()` boundary or at end-of-job — they never
  reach the rpascript.
- **Only the last range flushes:** two `power_range()` calls before a cut
  emit only the LAST range (each call replaces the pending values and
  warnings, never extends).
- **Ordering guarantee:** when both are pending, the flush emits
  `CUT_SPEED_LASER_1` first, then the power block — so the power minimum
  is scaled against the speed that is about to be cut.
- **Sticky scaling:** a mid-layer `cut_speed()` change WITHOUT a subsequent
  `power_range()` does not re-scale already-emitted power. The power lines
  were flushed (and scaled) at the previous cut; only a new `power_range()`
  re-emits them, scaled against the speed at that flush.
- **Corrupted-config error surfaces at the cut:** `_effective_min_power`
  is only called from the flush, so a corrupted `max_cut_speed` (non-finite
  or <= 0 via direct assignment) raises `ValueError("max_cut_speed must be
  > 0")` at the `cut_*` action — not at `power_range()` — and only when a
  cut follows.

The transcript lines for `power_range()`/`cut_speed()`/`cut_*` are
unchanged, so replay determinism (full re-stage and delta re-stage) is
preserved: the server replays the transcript and the flush happens during
the replay of the `cut_*` line.

## Setters

Setters for the configuration variables are exposed in the RPC interface and via the `/power_scale` TUI command:

- `set_max_cut_speed(speed)` — raises `ValueError` unless `speed > 0`.
- `set_power_floor(floor)` — raises `ValueError` unless `0 <= floor <= 100`.
- `set_power_scaling_enabled(enabled)` — coerces to `bool`.

## Emission behavior

At the flush (the next `cut_*` action), after resolving `min_power`/`max_power`:

- When `power_scaling_enabled` is True: `effective = _effective_min_power(resolved_min_power, resolved_max_power, self._current_layer_speed)`; the emitted minimum is `min(effective, resolved_max_power)` — the clamp is enabled-path-only.
- When `power_scaling_enabled` is False: the emitted minimum is the resolved minimum, unchanged — NO clamp (`power_range(70, 50)` still emits `[70, 50]` with a warning).
- `LAYER_MAX_POWER_1` always carries the resolved maximum.
- The layer's declared minimum (`declare_layer()`'s `min_power_1`) is never scaled; only the `power_range()` emission is.

The scaling is computed at flush time from the pending range and the
current layer speed (see "Deferred emission (flush at cut)" above), not at
`power_range()` call time.

## Warning thresholds

The hard-coded 8% warning thresholds in `declare_layer()` and `power_range()` are replaced with `self.power_floor`, and the floor is interpolated into the warning text ("below {floor}%"). The default floor stays 8.0.

## Parameter rename

The `power_range` method accepts parameters named `min` and `max`. These conflict with the functions min() and max() and are renamed to `min_power` and `max_power` respectively. The rename is applied through the RPC chain (`RpcRdDriver.power_range`, `exposed_power_range`, `gluescript_power_range`). The transcript line stays positional (`power_range(10.0, 70.0)`) so persisted `.cglu` files replay unchanged.