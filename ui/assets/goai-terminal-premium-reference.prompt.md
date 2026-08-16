# GOAI Premium Terminal Reference

- Generator: built-in `image_gen` (`gpt-image-2` path)
- Use case: `ui-mockup`
- Intended use: visual direction reference for the native desktop application
- Status: approved and implemented as the current desktop shell

## Final prompt

```text
Use case: ui-mockup
Asset type: high-fidelity desktop financial research workstation reference image, full-screen application screenshot, 16:9 landscape
Primary request: Create a genuinely premium, shippable desktop interface for GOAI, an institutional Hong Kong and US equity options research workstation. This is a visual reference for a native desktop application, not a website, not concept art, and not a generic AI dashboard.
Scene/backdrop: edge-to-edge application canvas only; no laptop frame, no desk, no device mockup, no surrounding environment.
Style/medium: realistic production UI mockup with extremely crisp typography, disciplined information architecture, restrained luxury, inspired by professional market terminals and editorial financial software without copying any proprietary product.
Composition/framing: 16:9 full screen. A 42px top command strip; a narrow 184px left workspace index; a large central trading/research canvas; a 300px fixed right decision ledger; a 36px bottom status and research-condition strip. The central canvas should feel substantial: quote tape across the top, a large market/volatility chart and compact order-book/Greeks pane side by side, a dense option chain below, then a thin risk and event strip. The right side is one continuous ledger separated by horizontal rules, not stacked cards.
Subject/details: show a Tencent options research scenario with precise, believable numeric information. Main heading text exactly: "腾讯控股 0700.HK". Top command text exactly: "0700 HK <GO>". Brand text exactly: "GOAI". Key labels, rendered verbatim where used: "总览", "期权链", "事件", "资料", "风险", "行情", "波动", "到期损益", "结论", "风险预算", "证据", "研究条件". Main price: "478.80". Decision: "这次先不交易". Use remaining rows primarily as aligned numbers, dates, short Chinese labels, bid/ask columns, IV, Delta, volume and open-interest values; avoid lorem ipsum or fake prose.
Visual hierarchy: data-dense but calm. One clear selected module, one primary chart, one option-chain table, one persistent decision ledger. Precise column alignment, subtle row banding, sharp 1px dividers, narrow headers, monospaced financial numerals, high-quality Chinese sans-serif labels. No oversized title or empty hero area.
Color palette: near-black ink #05080A, blue-black #0A1116, graphite #10191E, subtle steel separators #26343B. One cool cyan accent #7CC6D2 only for selection and focus. Green #24C06A only for positive/bid values, red #E6554D only for losses/ask values. Off-white #D8DEDC and muted gray #7E8A89 for typography. Absolutely no amber, orange, purple, neon, or rainbow color.
Materials/textures: matte screen surfaces, very subtle tonal depth and micro-contrast; no visible gradient effects, no glossy glass, no glowing edges.
Lighting/mood: sober, confident, institutional, precise, quietly expensive; designed for long professional sessions.
Constraints: practical native desktop layout; every region must have a clear operational purpose; keep text readable and aligned; no rounded card grid; no floating cards; no chat bubbles; no AI assistant avatar; no sparkle icons; no pill badge collection; no decorative blobs; no glassmorphism; no giant sidebar; no huge empty margins; no marketing slogan; no fake 3D; no watermark; no third-party logos or trademarks; do not imitate Bloomberg branding or proprietary layout exactly.
Avoid: generic SaaS dashboard aesthetic, Dribbble-style cards, cyberpunk terminal, orange/amber typography, excessive borders, ornamental metrics, random English micro-labels, illegible glyphs, fake code, mobile UI.
```

## Implementation notes

- Treat the image as a composition and hierarchy reference, not a literal source of financial data.
- Replace any generated label errors with product-approved Chinese copy during implementation.
- Preserve the existing deterministic decision pipeline and simulation-only boundary.
