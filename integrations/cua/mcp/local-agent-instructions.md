# Desktop control rules (Cua Driver)

You operate Windows apps only through the `cua` MCP tools. Follow this loop for every step:

1. Observe: call `get_window_state` with the exact `pid` and `window_id`, plus `max_elements: 40` and `max_depth: 6`.
2. If an element in the tree matches the target, act on its `element_index`. This is fastest and most reliable.
3. Otherwise call `parse_visual_regions` with that result's `capture_id`. Pick exactly one region whose text matches the target.
   If none matches, or more than one does, stop and say so. Do not guess coordinates.
4. Click the centre of that region (`x + w//2`, `y + h//2`) and pass the same `capture_id`. A capture allows only one action and expires after 60 seconds.
5. Observe again after every action, including refusals, before deciding anything else. Confirm the result from the new state.

Refusals:
- `capture_not_found`, `capture_expired` or `capture_generation_mismatch`: observe again. Never retry without `capture_id`.
- `background_unavailable`: stop and report it. Do not switch to foreground delivery unless the task says foreground is allowed.
- `bounded_resource_outside_manifest` or `permission_denied`: stop. That app, path or tool is not allowed.
- `list_windows` refused: call `launch_app` and use the `pid` and `window_id` it returns.

OCR can misread text, especially digits. Never submit, pay, delete or send based only on OCR text. Confirm the value from the app's own state, or stop and ask.
