# Avatar reuse: licence preflight

**Checked:** 2026-11-25. **Status:** candidate review, not a dependency,
asset, identity, or release decision. No component was imported or built.
The [product brief](../PRODUCT_BRIEF.md#16-phases) keeps the desktop and
native phone apps ahead of the optional desk pet.

## Code, runtime and asset rights are separate

| Candidate | Evidence | Implication for Kelp |
| --- | --- | --- |
| AIRI | The [v0.15.4 root licence][airi] and the [`pipelines-audio`][audio], [`stage-ui-three`][three] and [`stage-ui-live2d`][airi-live2d] package manifests say MIT. Its [Spine package notice][spine] explicitly separates the MIT code from Spine Runtimes. | Selected MIT code can be considered with its copyright and permission notice. Check dependencies, model weights, voices and art separately. The renderer packages are not drop-in replacements for Kelp's React client. |
| Open-LLM-VTuber | The [root licence][vtuber] is MIT but explicitly excludes Live2D sample models. The [sample terms][sample] and [Free Material License][material] apply independently. | Evaluate reusable code separately. Do not bring in `live2d-models/` or redistribute Mao or Shizuku under the MIT grant. |
| Live2D Cubism | [Cubism Core][core] is proprietary. The Framework uses a [custom source-available licence][framework]. Live2D publishes separate [release terms][release] for SDK content, including AI/chatbots. | An MIT wrapper does not cover Core, Framework or art. Confirm with Live2D what applies to the exact distribution and publisher before shipping. |
| `three-vrm` | The [renderer licence][three-vrm] is MIT. The [VRM 1.0 metadata spec][meta] and [VRM Public License][vrm] describe separate model-level terms where applicable. | A renderer licence does not grant rights in an avatar. Check the actual file, its `licenseUrl`, permissions, credit and any separate grant. |

## Before selecting any Phase 3 asset or renderer

- **Do not assume a Live2D exemption.** A fixed bundled model and arbitrary
  user model import are different scenarios. Live2D describes indefinite
  model expansion under its [Expandable Applications][expandable] rules,
  with separate review and an agreement before release even for otherwise
  exempt publishers. Ask Live2D for a written determination if arbitrary
  model import or avatar sharing is in scope. This preflight does not
  determine Kelp's category, fee, or eligibility.
- **Don't treat loading a model as permission to distribute it.** For a
  VRM 1.0 asset, check avatar use, commercial use, credit, modification and
  redistribution separately. The metadata defaults are restrictive
  (`onlyAuthor`, `personalNonProfit`, required credit, no redistribution,
  modification prohibited). Do not treat missing metadata as permission
  or displayed metadata as proof the uploader holds the rights.
- **Track each item we ship or fetch.** Record the upstream revision or
  asset digest, licensor, exact paths, licence, source, required
  attribution, allowed uses and whether sharing is permitted. Include
  transitive runtime dependencies, sample art and voice models. Prefer
  sharing a persona configuration that references an asset separately
  over re-distributing raw model files without explicit permission.

The general licence terms have been checked. The [Phase 0 checklist][brief]
item remains open for the specific parts to be reused. No renderer or sample
asset was selected, no contributor rights were inferred, and Kelp's own
[release licence](../RELEASE_LICENSING.md) is a separate owner decision.

[airi]: https://github.com/moeru-ai/airi/blob/v0.15.4/LICENSE
[audio]: https://github.com/moeru-ai/airi/blob/v0.15.4/packages/pipelines-audio/package.json
[three]: https://github.com/moeru-ai/airi/blob/v0.15.4/packages/stage-ui-three/package.json
[airi-live2d]: https://github.com/moeru-ai/airi/blob/v0.15.4/packages/stage-ui-live2d/package.json
[spine]: https://github.com/moeru-ai/airi/blob/v0.15.4/packages/stage-ui-spine/LICENSE.md
[vtuber]: https://github.com/Open-LLM-VTuber/Open-LLM-VTuber/blob/main/LICENSE
[sample]: https://www.live2d.com/en/learn/sample/model-terms/
[material]: https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html
[core]: https://docs.live2d.com/en/cubism-sdk-manual/cubism-core
[framework]: https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html
[release]: https://www.live2d.com/en/sdk/license/
[expandable]: https://www.live2d.com/en/sdk/license/expandable/
[three-vrm]: https://github.com/pixiv/three-vrm/blob/dev/LICENSE
[vrm]: https://vrm.dev/en/licenses/1.0
[meta]: https://github.com/vrm-c/vrm-specification/blob/master/specification/VRMC_vrm-1.0/meta.md
[brief]: ../PRODUCT_BRIEF.md#17-phase-0-exit-criteria-and-open-questions
