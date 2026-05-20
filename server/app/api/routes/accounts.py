from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.models import Account as AccountModel, User
from server.app.schemas.account import (
    AccountBrowserSessionFinishRead,
    AccountBrowserSessionRead,
    AccountCheckRequest,
    AccountExportRequest,
    AccountRead,
    AccountRenameRequest,
    PlatformLoginRequest,
)
from server.app.modules.accounts import (
    check_account,
    delete_account,
    export_accounts_auth_package,
    finish_account_login_session,
    get_account,
    get_login_session_status,
    import_accounts_auth_package,
    list_accounts,
    rename_account,
    register_account_from_storage_state,
    relogin_account,
    start_account_login_session,
    start_login_session,
    stop_account_login_session,
)
from server.app.api.serializers import to_account_read
from server.app.modules.tasks.drivers import all_driver_codes, get_driver

router = APIRouter()


def _verify_account_ownership(account: AccountModel | None, current_user: User) -> AccountModel:
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if current_user.role != "admin" and account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


def _to_browser_session_read(result) -> AccountBrowserSessionRead:
    return AccountBrowserSessionRead(
        account=to_account_read(result.account),
        platform_code=result.platform_code,
        account_key=result.account_key,
        session_id=result.session_id,
        novnc_url=result.novnc_url,
    )


def _verify_platform_code(platform_code: str) -> str:
    if platform_code not in all_driver_codes():
        raise HTTPException(status_code=404, detail="Unknown platform")
    return platform_code


@router.get("/platforms")
def read_account_platforms() -> list[dict[str, str]]:
    platforms = []
    for code in all_driver_codes():
        driver = get_driver(code)
        platforms.append({"code": driver.code, "name": driver.name})
    return platforms


# 获取所有账号列表
@router.get("", response_model=list[AccountRead])
def read_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AccountRead]:
    accounts = list_accounts(db)
    if current_user.role != "admin":
        accounts = [a for a in accounts if a.user_id == current_user.id]
    return [to_account_read(account) for account in accounts]


# 添加平台账号（复用已保存状态，use_browser 必须为 False）
@router.post("/{platform_code}/login", response_model=AccountRead)
def login_platform_account(
    platform_code: str,
    payload: PlatformLoginRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    platform_code = _verify_platform_code(platform_code)
    return to_account_read(register_account_from_storage_state(db, current_user.id, platform_code, payload))


# NOTE: /{account_id:int}/login-session routes MUST appear before /{platform_code}/login-session
# because both path patterns match the same shape; the :int converter narrows the regex to [0-9]+
# so numeric IDs route correctly when this route is first.

@router.post("/{account_id:int}/login-session", response_model=AccountBrowserSessionRead)
def start_existing_account_login_session_endpoint(
    account_id: int,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    return _to_browser_session_read(start_account_login_session(db, account, payload or AccountCheckRequest()))


@router.get("/{account_id:int}/login-session/{session_id}/status")
def get_login_session_status_endpoint(
    account_id: int,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    request = get_login_session_status(db, account, session_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Login session not found")
    return {
        "status": request.status,
        "novnc_url": request.novnc_url,
        "error_message": getattr(request, "error_message", None),
        "browser_session_id": request.browser_session_id,
    }


@router.post("/{account_id:int}/login-session/{session_id}/finish", response_model=AccountBrowserSessionFinishRead)
def finish_existing_account_login_session_endpoint(
    account_id: int,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionFinishRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    updated, result = finish_account_login_session(db, account, session_id)
    return AccountBrowserSessionFinishRead(
        account=to_account_read(updated),
        logged_in=result.logged_in,
        url=result.url,
        title=result.title,
    )


@router.delete("/{account_id:int}/login-session/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def stop_existing_account_login_session_endpoint(
    account_id: int,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    stop_account_login_session(db, account, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{platform_code}/login-session", response_model=AccountBrowserSessionRead)
def start_platform_login_session_endpoint(
    platform_code: str,
    payload: PlatformLoginRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionRead:
    platform_code = _verify_platform_code(platform_code)
    return _to_browser_session_read(start_login_session(db, current_user.id, platform_code, payload))


# 导出账号授权包（含 Playwright storage_state 的 ZIP）
@router.post("/export")
def export_accounts(
    payload: AccountExportRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> FileResponse:
    export_path = export_accounts_auth_package(db, payload or AccountExportRequest())
    return FileResponse(
        export_path,
        media_type="application/zip",
        filename=export_path.name,
    )


# 导入账号授权包
@router.post("/import")
async def import_accounts(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    import io
    import re
    import zipfile

    from server.app.core.config import MAX_ZIP_BYTES

    zip_bytes = await file.read()
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"ZIP file exceeds {MAX_ZIP_BYTES // (1024 * 1024)}MB limit",
        )

    ZIP_ENTRY_RE = re.compile(r"^(?:manifest\.json|accounts/[a-zA-Z0-9_]+-\d+/(?:account|storage_state)\.json)$")
    MAX_ENTRIES = 50
    MAX_ENTRY_BYTES = 2 * 1024 * 1024

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            entries = archive.namelist()
            if len(entries) > MAX_ENTRIES:
                raise HTTPException(status_code=400, detail=f"ZIP contains {len(entries)} entries, max {MAX_ENTRIES} allowed")
            for entry_name in entries:
                info = archive.getinfo(entry_name)
                if not ZIP_ENTRY_RE.match(entry_name):
                    raise HTTPException(status_code=400, detail=f"Invalid ZIP entry path: {entry_name}")
                if info.file_size > MAX_ENTRY_BYTES:
                    raise HTTPException(status_code=400, detail=f"ZIP entry too large: {entry_name} ({info.file_size} bytes)")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    return import_accounts_auth_package(db, current_user.id, zip_bytes)


# 校验指定账号的登录状态
@router.post("/{account_id:int}/check", response_model=AccountRead)
def check_existing_account(
    account_id: int,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    return to_account_read(check_account(db, account, payload or AccountCheckRequest()))


# 重新从已保存 storage_state 注册账号
@router.post("/{account_id:int}/relogin", response_model=AccountRead)
def relogin_existing_account(
    account_id: int,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    return to_account_read(relogin_account(db, account, payload or AccountCheckRequest()))


# 重命名账号显示名称
@router.patch("/{account_id:int}", response_model=AccountRead)
def rename_existing_account(
    account_id: int,
    payload: AccountRenameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    return to_account_read(rename_account(db, account, payload.display_name))


# 删除指定账号
@router.delete("/{account_id:int}", status_code=status.HTTP_204_NO_CONTENT)
def delete_existing_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    from server.app.shared.errors import ClientError
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    try:
        delete_account(db, account)
        db.commit()
    except ClientError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="删除账号失败: " + str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
