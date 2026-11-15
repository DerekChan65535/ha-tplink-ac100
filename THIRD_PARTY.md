# Third-party references and assets

## Project protocol implementation

The TL-AC100 client is independently implemented for this project. Observed endpoint
shapes and behavior are interface facts, not permission to copy another project's
implementation. Third-party protocol source with no applicable license must not be
copied, translated, or renamed into this repository.

Do not distribute controller firmware, extracted proprietary web scripts, raw device
responses, or packet captures as project source or fixtures. Fixtures must be synthetic
or have clear permission and be redacted. A reference's license does not automatically
license assets or firmware linked from it.

## Framework and development tools

Home Assistant and its official development documentation define the integration API:

- https://www.home-assistant.io/
- https://contributors.home-assistant.io/core/integrations/
- https://www.hacs.xyz/docs/publish/

Dependencies keep their own licenses. The root MIT license covers this project's own
contributions, not Home Assistant, HACS, Python packages, firmware, or trademarks.
Runtime/development dependencies and exact resolved versions are recorded in
pyproject.toml and uv.lock.

## Contribution requirement

If a change uses an external reference or asset, record its URL, version/commit,
license checked, and precise use here before merging. Retain all required notices
for licensed reuse. If permission or provenance is unclear, do not include the material.
Do not cite private device URLs, identifiers, credentials, or raw evidence in public docs.

TP-LINK and Home Assistant names are used only to identify compatible products. The
project is not affiliated with or endorsed by their owners.
