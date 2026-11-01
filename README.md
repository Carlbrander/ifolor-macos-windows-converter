# ifolor-convert

Convert [ifolor Designer](https://www.ifolor.ch) photo book projects between the Windows and macOS versions of the app.
The two apps use different, incompatible project formats, so a book started on one platform can't normally be opened on the other.

Unofficial; not affiliated with ifolor. Keep a backup of your original project.

## Requirements

- Python 3.8+ (standard library only)
- On macOS, the built-in `sips` tool is used to convert HEIC photos to JPEG. On Windows, the project's high-resolution previews are used instead.

## Usage

```sh
# Windows project folder (contains Project.ipp) -> macOS
python3 ifolor_convert.py --windows_to_macos "path/to/My Book"

# macOS project (.ifolordesigner) -> Windows
python3 ifolor_convert.py --macos_to_windows "path/to/My Book.ifolordesigner"
```

The result is written next to the input, as `My Book (macOS).ifolordesigner` or `My Book (Windows)`, together with a short README explaining where to put it.

- **macOS:** quit ifolor Designer and move the `.ifolordesigner` package into
  `~/Library/Group Containers/ZKENARDFWN.group.com.ifolor.designer/Library/Documents`, then open it in the app.
- **Windows:** copy the output folder to the PC and open `Project.ipp` in ifolor Designer.

Check the pages and save once after opening.

## What is converted

- Pages, cover and spine text
- Photos, with their position, size, crop and rotation (HEIC is converted to JPEG for macOS)
- Text boxes: fonts, sizes, colours, bold/italic, alignment, line breaks, paragraph indents and tab positions

## Limitations

- The built-in templates are for an A4 portrait photo book. For other book types, pass an empty project of the **target** format with the same product via `--template` (untested).
- Brightness/contrast/saturation adjustments are not transferred (the converter prints a note).
- Windows-only fonts (Yu Gothic UI, Segoe UI) are replaced with Trebuchet MS on macOS.
- Backgrounds, clip art, frames and other decorations are not converted.
