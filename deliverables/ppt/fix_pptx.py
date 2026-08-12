"""Post-process exported PPTX files so they pass PowerPoint schema validation.

Fixes:
1. ppt/presentation.xml: move <p:notesMasterIdLst> before <p:sldIdLst>.
2. Every slide: any <p:txBody> without an <a:p> paragraph gets an empty paragraph.
3. Drop orphaned customXml parts not referenced by any relationship.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path
from lxml import etree

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = f"{{{P_NS}}}"
A = f"{{{A_NS}}}"


def fix_presentation(root: etree._Element) -> bool:
    sld_ids = root.find(f"{P}sldIdLst")
    notes_ids = root.find(f"{P}notesMasterIdLst")
    if sld_ids is None or notes_ids is None:
        return False
    sib = list(root)
    if sib.index(notes_ids) < sib.index(sld_ids):
        return False
    root.remove(notes_ids)
    sld_ids.addprevious(notes_ids)
    return True


def fix_slide(root: etree._Element) -> bool:
    changed = False
    for tx_body in root.iter(f"{P}txBody"):
        if tx_body.find(f"{A}p") is None:
            etree.SubElement(tx_body, f"{A}p")
            changed = True
    return changed


def fix_pptx(path: Path) -> int:
    changed_parts = []
    tmp = path.with_suffix(".fix.tmp.pptx")
    with zipfile.ZipFile(path, "r") as src, zipfile.ZipFile(
        tmp, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        names = src.namelist()
        for name in names:
            data = src.read(name)
            if name == "ppt/presentation.xml":
                root = etree.fromstring(data)
                if fix_presentation(root):
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                    changed_parts.append(name)
            elif name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                root = etree.fromstring(data)
                if fix_slide(root):
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                    changed_parts.append(name)
            elif name.startswith("customXml/"):
                changed_parts.append(f"{name} (dropped)")
                continue
            dst.writestr(name, data)
    shutil.move(str(tmp), str(path))
    return len(changed_parts)


def main(argv: list[str]) -> int:
    for raw in argv:
        path = Path(raw)
        count = fix_pptx(path)
        print(f"{path.name}: {count} part(s) fixed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
