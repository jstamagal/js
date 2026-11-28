---
name: h3-image-prompt-writing
description: Write prompts for MiniMax-H3-Image text-to-image generation and image editing, and optionally render them through the MiniMax API. Use when the user wants an image such as a poster, infographic, chart, product shot, UI screen, character, scene, storyboard or grid, wants to improve an image prompt, or wants to edit, combine or restyle reference images with MiniMax-H3-Image.
SOURCE: https://github.com/MiniMax-AI/MiniMax-H3-Image/tree/40850901ab938303b5274122b5f305ebc5b75c80/skills/h3-image-prompt-writing
---

# MiniMax-H3-Image Prompt Writing

MiniMax-H3-Image renders best from a complete description of the finished image: what the image is
for, what is in it and where, how it looks, and every word that appears on it. This skill turns a
short request into that description, for text-to-image and for editing with up to 16 reference
images, and can send the result to the MiniMax API.

## Workflow

1. **Pick the task.** Use text-to-image when there are no input images. Use editing when the user
   supplies images to change, combine or borrow from. Read the matching guide and its examples
   before writing:
   - [references/t2i-en.txt](references/t2i-en.txt) for text-to-image.
   - [references/edit-en.txt](references/edit-en.txt) for editing. Look at every input image, in
     the order supplied.
2. **Plan the image** in this order: purpose and deliverable, overall layout, aspect ratio, the
   subjects and what they are doing, the visual style, the text. Fill ordinary gaps yourself; a
   short idea is enough. Write requested copy yourself, mark example data as illustrative and keep
   an unseen asset as a named reference. Ask only when a missing fact would make the prompt wrong,
   such as a chart of the user's own figures with none given.
3. **Write the prompt** with the rules below and the guide.
4. **Check it** against the request with the [final check](#final-check).
5. **Deliver** it in the [output format](#output-format). When the user asks for the image itself,
   [render it](#rendering-the-prompt). If `MINIMAX_API_KEY` is not set, deliver the prompt and say
   that setting the key lets the skill render it.

## Writing rules

### Basic description

- **Task before style.** Open with what the image is and what it is for, then the main people or
  objects and their state or action. Style comes after.
- **Layout before details.** Give the number of subjects, their positions, the viewpoint and the
  empty space. For dense content, name the regions first and then fill each one: "a title across
  the top; below it, a photo on the left and three short paragraphs on the right."
- **Style through visible details.** Write color, material, light and texture, such as "soft side
  light, brushed-metal reflections, generous white space", instead of words like "premium" or
  "high-end". Keep the details that matter and drop contradictory ones.

### Text and information

- **Quote every word that appears.** Put the complete text in quotes, in its own language and with
  its spacing as given, together with its position, size hierarchy and line breaks. When the user
  wants you to write the copy, write the actual words.
- **Say when there is no text.** In a text-to-image prompt, add "The image contains no text,
  letters or numbers." so the model does not add titles, labels or decorative lettering. In an
  edit, say that no other lettering is added, so text in the source image is kept.
- **Get data right.** For charts, academic figures and educational images, give exact values,
  units and relationships, and the time range for anything that changes over time. Mark example
  data as illustrative and never present invented numbers as real.

### Reference images and editing

- **Name the inputs.** Refer to the images as `<Picture 1>`, `<Picture 2>`, ... in the order they
  are supplied.
- **Give each reference a role.** Say what each image contributes, what must stay consistent and
  what may change. When only a color is borrowed, say that the reference's shape and material are
  not.
- **Separate what changes from what stays.** Locate the target by position or features, describe
  what it becomes and how far the change reaches, then name what is preserved: "the cup on the
  left becomes blue; its shape, material, position and size stay the same."
- **Describe the finished result.** Say whether something is moved, copied, replaced or removed,
  and what fills the space a removed object leaves. When text or data changes, give the complete
  new content and say whether the original layout and chart structure stay.
- **One clear goal per round.** In multi-round editing, name the image used in this round, what
  changes and what stays. Avoid references like "the one from before".
- **Grids by rows and columns.** Write "2 rows and 3 columns" or "3×3 (rows × columns)", not
  "2×3". Describe irregular layouts directly, such as "3 panels on top and 2 below", and address
  one cell as "row 2, column 1".

## Output format

Give the prompt in a text block, followed by the aspect ratio:

````text
```text
<the prompt>
```

Aspect ratio: 3:4
````

- Write the description in English. Text that appears in the image keeps its own language.
- Text-to-image prompts are one or more flowing paragraphs. Edit prompts are one paragraph in the
  order the edit guide sets out: source definitions, then the target and each source's role, then
  the changed details.
- The aspect ratio is width:height between 1:3 and 3:1. Keep a ratio the user gives, or use the
  nearest allowed one and say so when it is outside that range; otherwise choose it from the
  layout. For an edit that keeps the input's framing, use the input's ratio.
- When the user asks for options, give up to three prompts with clearly different takes, each with
  its own aspect ratio.

## Rendering the prompt

### MiniMax API

[scripts/generate.py](scripts/generate.py) sends a prompt to the MiniMax API and saves the images.
It uses only the Python standard library. The user sets the key in `MINIMAX_API_KEY`, and
`MINIMAX_API_BASE=https://api.minimax.cn` for the China platform; the script defaults to
`https://api.minimax.io`. Do not change either variable.

Save only the prompt text, without the "Aspect ratio" line, to a file and pass the ratio
separately:

```bash
# Text-to-image
python3 scripts/generate.py --prompt-file prompt.txt --aspect-ratio 3:4 --out poster

# Editing: images in <Picture N> order, as local files or URLs
python3 scripts/generate.py --prompt-file edit.txt --image room.jpg --image sofa.png \
  --aspect-ratio 4:3 --out room_edit
```

- For text-to-image the script sets `disable_prompt_enhanced: true`, so the API renders the
  prompt as written at the given aspect ratio. Pass `--enhance` to let the API expand a short
  prompt instead; it then also chooses the ratio when none is given.
- The API always expands editing prompts. Pass the aspect ratio for edits too; without it the API
  chooses one. It accepts up to 16 images in JPG, PNG, WebP, HEIC or HEIF, each at most 64 MiB with
  sides of 128 to 8192 pixels.
- The output area defaults to `4K`, which gives the best results. `--area 2K` or `--area 1K` is
  faster.
- `--n` asks for up to 10 images per request. Dense text, charts and diagrams vary from run to
  run, so render two to four of them and keep the best.
- Each image is saved as `<out>_<i>.png`. When the API expanded the prompt, the expanded text is
  saved next to it as `<out>_<i>.revised.txt`.

### Your own deployment

On a local SGLang server, send the prompt as `prompt` and a `size` (`WIDTHxHEIGHT`) for the
aspect ratio from the [Sizes table](https://github.com/MiniMax-AI/MiniMax-H3-Image#sizes); the
other fields are under
[Request Parameters](https://github.com/MiniMax-AI/MiniMax-H3-Image#request-parameters), and
vLLM-Omni's differ slightly, see the [vLLM-Omni recipe](https://recipes.vllm.ai/MiniMaxAI/MiniMax-H3-Image).
A local server does not expand prompts, so a prompt written with this skill takes the place of
that step.

## Final check

- Every word that appears is quoted and spelled exactly as given, and no text is added when none
  was asked for.
- Counts, positions, left and right, foreground and background, and contact between people and
  objects match the request.
- Values, units and labels are correct and complete.
- Each reference image has a role, and what is preserved is named.
- The aspect ratio fits the layout.
- No vague quality word remains where a visible detail can say it.
