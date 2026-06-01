from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from server.app.core.security import get_current_user, require_admin
from server.app.db.session import get_db
from server.app.modules.accounts.models import Account as AccountModel
from server.app.modules.audit.service import add_audit_entry
from server.app.modules.system.models import User
from server.app.modules.accounts.schemas import (
    AccountBrowserSessionFinishRead,
    AccountBrowserSessionRead,
    AccountCheckRequest,
    AccountExportRequest,
    AccountRead,
    AccountRenameRequest,
    LoginSessionStatusRead,
    PlatformLoginRequest,
    to_account_read,
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
from server.app.modules.tasks.drivers import all_driver_codes, get_driver

router = APIRouter()


def _verify_account_ownership(account: AccountModel | None, current_user: User) -> AccountModel:
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if current_user.role != "admin" and account.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


def _to_browser_session_read(result) -> AccountBrowserSessionRead:
    return AccountBrowserSessionRead(
        account=to_account_read(result.account),
        platform_code=result.platform_code,
        account_key=result.account_key,
        session_id=result.session_id,
        novnc_url=result.novnc_url,
        status=getattr(result, "status", None),
        queue_reason=getattr(result, "queue_reason", None),
    )


def _verify_platform_code(platform_code: str) -> str:
    if platform_code not in all_driver_codes():
        raise HTTPException(status_code=404, detail="未知平台")
    return platform_code


@router.get("/platforms")
def read_account_platforms() -> list[dict[str, str]]:
    platforms = []
    for code in all_driver_codes():
        driver = get_driver(code)
        platforms.append({"code": driver.code, "name": driver.name})
    return platforms


@router.get("", response_model=list[AccountRead])
def read_accounts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AccountRead]:
    accounts = list_accounts(db)
    if current_user.role != "admin":
        accounts = [a for a in accounts if a.user_id == current_user.id]
    return [to_account_read(account) for account in accounts]


@router.post("/{platform_code}/login", response_model=AccountRead)
def login_platform_account(
    platform_code: str,
    payload: PlatformLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    platform_code = _verify_platform_code(platform_code)
    account = register_account_from_storage_state(db, current_user.id, platform_code, payload)
    add_audit_entry(
        db,
        user=current_user,
        action="account.create",
        target_type="account",
        target_id=account.id,
        payload={"platform_code": platform_code, "display_name": account.display_name},
        request=request,
    )
    return to_account_read(account)


# NOTE: /{account_id:int}/login-session routes MUST appear before /{platform_code}/login-session
@router.post("/{account_id:int}/login-session", response_model=AccountBrowserSessionRead)
def start_existing_account_login_session_endpoint(
    account_id: int,
    request: Request,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    result = start_account_login_session(db, account, payload or AccountCheckRequest())
    add_audit_entry(
        db,
        user=current_user,
        action="account.login_session.start",
        target_type="account",
        target_id=account_id,
        payload={"session_id": result.session_id},
        request=request,
    )
    return _to_browser_session_read(result)


@router.get("/{account_id:int}/login-session/{session_id}/status", response_model=LoginSessionStatusRead)
def get_login_session_status_endpoint(
    account_id: int,
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoginSessionStatusRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    request = get_login_session_status(db, account, session_id)
    if request is None:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    return LoginSessionStatusRead(
        status=request.status,
        novnc_url=request.novnc_url,
        error_message=request.error_message,
        queue_reason=request.queue_reason,
        browser_session_id=request.browser_session_id,
    )


@router.post("/{account_id:int}/login-session/{session_id}/finish", response_model=AccountBrowserSessionFinishRead)
def finish_existing_account_login_session_endpoint(
    account_id: int,
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionFinishRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    updated, result = finish_account_login_session(db, account, session_id)
    add_audit_entry(
        db,
        user=current_user,
        action="account.login_session.finish",
        target_type="account",
        target_id=account_id,
        payload={"session_id": session_id, "result": {"logged_in": result.logged_in}},
        request=request,
    )
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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    stop_account_login_session(db, account, session_id)
    add_audit_entry(
        db,
        user=current_user,
        action="account.login_session.abort",
        target_type="account",
        target_id=account_id,
        payload={"session_id": session_id},
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{platform_code}/login-session", response_model=AccountBrowserSessionRead)
def start_platform_login_session_endpoint(
    platform_code: str,
    payload: PlatformLoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountBrowserSessionRead:
    platform_code = _verify_platform_code(platform_code)
    result = start_login_session(db, current_user.id, platform_code, payload)
    add_audit_entry(
        db,
        user=current_user,
        action="account.login_session.start",
        target_type="account",
        target_id=None,
        payload={"platform_code": platform_code, "session_id": result.session_id},
        request=request,
    )
    return _to_browser_session_read(result)


@router.post("/export")
def export_accounts(
    request: Request,
    payload: AccountExportRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> FileResponse:
    effective_payload = payload or AccountExportRequest()
    export_path = export_accounts_auth_package(db, effective_payload)
    account_ids = list(getattr(effective_payload, "account_ids", None) or [])
    add_audit_entry(
        db,
        user=current_user,
        action="account.export",
        target_type="account",
        target_id=None,
        payload={"account_ids": account_ids, "count": len(account_ids)},
        request=request,
    )
    return FileResponse(
        export_path,
        media_type="application/zip",
        filename=export_path.name,
    )


@router.post("/import")
async def import_accounts(
    request: Request,
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
            detail=f"ZIP 文件超过 {MAX_ZIP_BYTES // (1024 * 1024)}MB 限制",
        )

    ZIP_ENTRY_RE = re.compile(r"^(?:manifest\.json|accounts/[a-zA-Z0-9_]+-\d+/(?:account|storage_state)\.json)$")
    MAX_ENTRIES = 50
    MAX_ENTRY_BYTES = 2 * 1024 * 1024

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            entries = archive.namelist()
            if len(entries) > MAX_ENTRIES:
                raise HTTPException(status_code=400, detail=f"ZIP 文件包含 {len(entries)} 个条目，最多允许 {MAX_ENTRIES} 个")
            for entry_name in entries:
                info = archive.getinfo(entry_name)
                if not ZIP_ENTRY_RE.match(entry_name):
                    raise HTTPException(status_code=400, detail=f"无效的 ZIP 条目路径：{entry_name}")
                if info.file_size > MAX_ENTRY_BYTES:
                    raise HTTPException(status_code=400, detail=f"ZIP 条目过大：{entry_name}（{info.file_size} 字节）")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="无效的 ZIP 文件")

    result = import_accounts_auth_package(db, current_user.id, zip_bytes)
    imported_count = len(result.get("imported", []) or [])
    add_audit_entry(
        db,
        user=current_user,
        action="account.import",
        target_type="account",
        target_id=None,
        payload={"imported_count": imported_count},
        request=request,
    )
    return result


@router.post("/{account_id:int}/check", response_model=AccountRead)
def check_existing_account(
    account_id: int,
    request: Request,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    updated = check_account(db, account, payload or AccountCheckRequest())
    add_audit_entry(
        db,
        user=current_user,
        action="account.check",
        target_type="account",
        target_id=account_id,
        payload={"result": {"status": getattr(updated, "status", None)}},
        request=request,
    )
    return to_account_read(updated)


@router.post("/{account_id:int}/relogin", response_model=AccountRead)
def relogin_existing_account(
    account_id: int,
    request: Request,
    payload: AccountCheckRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    updated = relogin_account(db, account, payload or AccountCheckRequest())
    add_audit_entry(
        db,
        user=current_user,
        action="account.relogin",
        target_type="account",
        target_id=account_id,
        payload=None,
        request=request,
    )
    return to_account_read(updated)


@router.patch("/{account_id:int}", response_model=AccountRead)
def rename_existing_account(
    account_id: int,
    payload: AccountRenameRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AccountRead:
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    old_display_name = account.display_name
    updated = rename_account(db, account, payload.display_name)
    new_display_name = updated.display_name
    if old_display_name != new_display_name:
        add_audit_entry(
            db,
            user=current_user,
            action="account.update",
            target_type="account",
            target_id=account_id,
            payload={
                "before": {"display_name": old_display_name},
                "after": {"display_name": new_display_name},
            },
            request=request,
        )
    return to_account_read(updated)


@router.delete("/{account_id:int}", status_code=status.HTTP_204_NO_CONTENT)
def delete_existing_account(
    account_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> Response:
    from server.app.shared.errors import ClientError
    account = _verify_account_ownership(get_account(db, account_id), current_user)
    display_name = account.display_name
    try:
        delete_account(db, account)
        db.commit()
    except ClientError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="删除账号失败: " + str(exc)) from exc
    add_audit_entry(
        db,
        user=current_user,
        action="account.delete",
        target_type="account",
        target_id=account_id,
        payload={"display_name": display_name},
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
