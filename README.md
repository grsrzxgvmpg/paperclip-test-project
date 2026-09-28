# Unit Converter

Simple Python unit converter — temperature, distance, weight.

## Usage

```python
from converter import celsius_to_fahrenheit, km_to_miles
print(celsius_to_fahrenheit(100))  # 212.0
print(km_to_miles(5))              # 3.106855
```

## Tests

```bash
pytest tests/ -v
```

## Integrations

- [`integrations/cua/`](integrations/cua/README.md): evaluation of Cua Perception, plus configs to drive Windows desktop apps from Claude Code, OpenClaw, Paperclip and local llama-server models through Cua Driver.
