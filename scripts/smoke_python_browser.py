from __future__ import annotations

import asyncio
from pathlib import Path

from formpilot.browser import PlaywrightFormBrowser
from formpilot.profile import ProfileStore
from formpilot.tools.form_tools import FormPilotTools


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    browser = PlaywrightFormBrowser(headless=True, profile_dir=root / ".formpilot/smoke-profile")
    try:
        await browser.start((root / "demo/index.html").as_uri())
        profile = ProfileStore.load(root / "examples/profile.example.json")
        tools = FormPilotTools(browser, profile, confirm=lambda _: True)
        page = await tools.inspect_page()
        by_label = {field["label"]: field for field in page["fields"]}
        name = by_label["姓名"]
        project = by_label["招生项目"]
        native_place = by_label["籍贯"]
        enrollment_date = by_label["入学时间"]
        name_result = await tools.fill_from_profile(name["field_id"], "identity.full_name_zh", None)
        project_result = await tools.fill_from_profile(project["field_id"], "application.project", None)
        rescanned = await tools.wait_and_rescan(50)
        batch = next(field for field in rescanned["fields"] if field["label"] == "招生批次")
        options = [option["text"] for option in batch["options"]]
        cascade_first = await tools.select_cascade_from_profile(
            native_place["field_id"],
            ["origin.province", "origin.city", "origin.district"],
            None,
        )
        cascade_approval = await tools.request_user_confirmation(cascade_first["target"], cascade_first["summary"])
        cascade_result = await tools.select_cascade_from_profile(
            native_place["field_id"],
            ["origin.province", "origin.city", "origin.district"],
            cascade_approval["approval_id"],
        )
        date_result = await tools.set_date_from_profile(enrollment_date["field_id"], "education.enrollment_date", None)
        assert name_result["ok"]
        assert project_result["ok"]
        assert cascade_result["ok"] and cascade_result["selected_levels"] == 3
        assert date_result["ok"] and date_result["mode"] == "calendar"
        assert "2026年秋季批次" in options
        assert all("current_value" not in field for field in rescanned["fields"])
        print(
            f"Python browser smoke test passed: {len(page['fields'])} fields, "
            f"dynamic options={options}, cascade=3 levels, custom calendar=navigated"
        )
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
