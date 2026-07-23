from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DOC_ROOTS = (
    Path("personal_info/PDF"),
    Path("personal_info"),
)

MERGED_DIR = Path(".formpilot/merged")

# requirement keyword -> filename keywords (any hit scores)
REQUIREMENT_ALIASES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"身份证|身份证件|证件扫描"), ("身份证", "身份")),
    (re.compile(r"学籍|学籍验证|学籍在线|教育部学籍"), ("学籍", "验证报告")),
    (re.compile(r"成绩单|本科成绩"), ("成绩单",)),
    (re.compile(r"绩点|排名证明|成绩排名"), ("绩点", "排名")),
    (re.compile(r"英语|外语|外国语|四六级|雅思|托福|语言成绩|语言能力|水平能力证明|CET|IELTS|TOEFL", re.I), ("英语", "外语", "外国语", "四六级", "雅思", "托福", "CET")),
    (re.compile(r"简历|个人简历|CV|curriculum", re.I), ("简历",)),
    (re.compile(r"奖项|获奖|竞赛|学术成果|论文|专利"), ("奖项", "学术", "成果")),
    (re.compile(r"照片|证件照|头像"), ("photo", "证件照", "照片")),
]


@dataclass(slots=True)
class LocalDocument:
    path: Path
    name: str
    suffix: str
    size: int

    def public(self) -> dict[str, Any]:
        return {
            "path": str(self.path).replace("\\", "/"),
            "name": self.name,
            "suffix": self.suffix,
            "size": self.size,
        }


def _iter_files(roots: list[Path]) -> list[LocalDocument]:
    seen: set[Path] = set()
    docs: list[LocalDocument] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            candidates = [root]
        else:
            candidates = sorted(
                p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")
            )
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            # Skip nested duplicates under PDF vs parent when same file name elsewhere is fine.
            seen.add(resolved)
            suffix = path.suffix.lower()
            if suffix not in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            docs.append(LocalDocument(path=path, name=path.name, suffix=suffix, size=size))
    return docs


def list_local_documents(*, roots: list[str | Path] | None = None) -> list[dict[str, Any]]:
    resolved_roots = [Path(r) for r in (roots or DEFAULT_DOC_ROOTS)]
    return [doc.public() for doc in _iter_files(resolved_roots)]


def _norm(text: str) -> str:
    return re.sub(r"[\s_\-—:：()（）\[\]【】]", "", str(text or "")).lower()


def suggest_documents_for_requirement(
    requirement: str,
    *,
    roots: list[str | Path] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Rank local files against a webpage upload requirement label/description."""
    wanted = str(requirement or "").strip()
    if not wanted:
        return {"ok": False, "error": "requirement 不能为空", "matches": []}
    docs = _iter_files([Path(r) for r in (roots or DEFAULT_DOC_ROOTS)])
    if not docs:
        return {
            "ok": False,
            "error": "本地 personal_info / personal_info/PDF 下未找到可用文件",
            "matches": [],
            "requirement": wanted,
        }

    wanted_norm = _norm(wanted)
    alias_keywords: list[str] = []
    for pattern, keys in REQUIREMENT_ALIASES:
        if pattern.search(wanted):
            alias_keywords.extend(keys)

    scored: list[dict[str, Any]] = []
    for doc in docs:
        name_norm = _norm(doc.name)
        score = 0
        reasons: list[str] = []
        if wanted_norm and wanted_norm in name_norm:
            score += 100
            reasons.append("文件名包含需求全文")
        for key in alias_keywords:
            if _norm(key) and _norm(key) in name_norm:
                score += 40
                reasons.append(f"命中别名:{key}")
        # Token overlap on CJK/english chunks from requirement.
        tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}|\d{2,}", wanted) if t]
        hit = 0
        for token in tokens:
            if _norm(token) in name_norm:
                hit += 1
        if hit:
            score += 12 * hit
            reasons.append(f"关键词重合:{hit}")
        if score <= 0:
            continue
        # Prefer PDFs for document uploads, images for photo.
        if "照片" in wanted or "头像" in wanted or "证件照" in wanted:
            if doc.suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                score += 15
                reasons.append("图片类型加分")
            name_l = doc.name.lower()
            if "photo" in name_l or "150" in name_l or "200" in name_l:
                score += 40
                reasons.append("证件照文件名加分")
            if name_l.startswith("image") and "photo" not in name_l:
                score -= 30
                reasons.append("通用image文件降权")
        elif doc.suffix == ".pdf":
            score += 10
            reasons.append("PDF类型加分")
        scored.append({**doc.public(), "score": score, "reasons": reasons})

    scored.sort(key=lambda item: (-int(item["score"]), item["name"]))
    top = scored[: max(1, min(limit, 20))]
    confident = bool(top) and int(top[0]["score"]) >= 50 and (
        len(top) == 1 or int(top[0]["score"]) >= int(top[1]["score"]) + 15
    )
    return {
        "ok": True,
        "requirement": wanted,
        "matches": top,
        "confident": confident,
        "hint": (
            "若 confident=true 可直接 upload_local_file；"
            "若需求是多份材料合并，用 merge_pdfs 后再 upload_local_file 并 preview_pdf_text 自检；"
            "若 matches 空/不自信，pause_for_user 通知人类。"
            if top
            else "无匹配；pause_for_user 请人类提供文件或说明。"
        ),
    }


def merge_pdfs(paths: list[str], *, output_name: str | None = None) -> dict[str, Any]:
    """Merge local PDF files into .formpilot/merged/ and return the output path."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return {
            "ok": False,
            "error": "缺少 pypdf 依赖，请先安装：pip install pypdf",
        }
    if not paths:
        return {"ok": False, "error": "paths 不能为空"}
    readers: list[tuple[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if not path.exists() or not path.is_file():
            return {"ok": False, "error": f"文件不存在：{path}"}
        if path.suffix.lower() != ".pdf":
            return {"ok": False, "error": f"只能合并 PDF：{path.name}"}
        try:
            readers.append((str(path), PdfReader(str(path))))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"无法读取 PDF {path.name}: {type(exc).__name__}"}

    writer = PdfWriter()
    page_count = 0
    for _label, reader in readers:
        for page in reader.pages:
            writer.add_page(page)
            page_count += 1

    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (output_name or "merged").strip()) or "merged"
    out = MERGED_DIR / f"{stem}.pdf"
    # Avoid clobbering blindly: add numeric suffix.
    if out.exists():
        idx = 2
        while True:
            candidate = MERGED_DIR / f"{stem}_{idx}.pdf"
            if not candidate.exists():
                out = candidate
                break
            idx += 1

    with out.open("wb") as handle:
        writer.write(handle)

    return {
        "ok": True,
        "path": str(out).replace("\\", "/"),
        "name": out.name,
        "source_count": len(readers),
        "page_count": page_count,
        "sources": [label for label, _ in readers],
        "hint": "合并完成后请 preview_pdf_text 检查内容，再 upload_local_file 上传。",
    }


def preview_pdf_text(path: str, *, max_chars: int = 4000) -> dict[str, Any]:
    """Extract text from a PDF for the model to verify contents before upload."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"ok": False, "error": "缺少 pypdf 依赖，请先安装：pip install pypdf"}
    file_path = Path(path).expanduser()
    if not file_path.is_absolute():
        file_path = (Path.cwd() / file_path).resolve()
    else:
        file_path = file_path.resolve()
    if not file_path.exists():
        return {"ok": False, "error": f"文件不存在：{file_path}"}
    if file_path.suffix.lower() != ".pdf":
        return {"ok": False, "error": "preview_pdf_text 仅支持 PDF"}
    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"无法读取 PDF：{type(exc).__name__}"}

    chunks: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        remain = max_chars - total
        if remain <= 0:
            break
        piece = text[:remain]
        chunks.append(f"[p{index + 1}] {piece}")
        total += len(piece)
    joined = "\n".join(chunks)
    return {
        "ok": True,
        "path": str(file_path).replace("\\", "/"),
        "page_count": len(reader.pages),
        "chars": len(joined),
        "truncated": total >= max_chars,
        "text": joined or "(未能提取到文本；可能是扫描件图片 PDF，请人工确认)",
        "hint": "请根据提取文本判断是否覆盖网页要求的材料；不确定则 pause_for_user。",
    }
