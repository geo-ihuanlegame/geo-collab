// server/app/modules/tasks/drivers/adapters/toutiao_publish.js
// Runs inside the live Toutiao editor page via page.evaluate(js, arg).
// arg = {
//   form: { <field>: <value>, ... },   // form.content has __GEO_IMG_k__ tokens
//   cover: { b64, mime } | null,
//   bodyImages: [{ token, b64, mime }, ...],
//   uploadUrl: "<UPLOAD_URL>",
//   publishUrl: "<PUBLISH_API_URL>",
// }
//
// Uses XMLHttpRequest so the page's global request hook (acrawler/secsdk)
// auto-appends a_bogus / msToken / _signature / x-secsdk-csrf-token to BOTH
// the image-upload POSTs and the publish POST.
//
// One round-trip does everything: upload cover + body images, mutate the form
// (pgc_feed_covers + __GEO_IMG_k__ -> <img>), then POST publish.
//
// Returns one envelope:
//   success: { ok:true,  step:"publish", uploads:[...rawUploads],
//              publish:{ httpStatus, data, raw } }
//   upload fail: { ok:false, step:"upload", index, httpStatus, raw }
async (arg) => {
  function b64ToBlob(b64, mime) {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime || "image/jpeg" });
  }

  function xhrPost(url, body, headers) {
    return new Promise((resolve) => {
      try {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", url, true);
        if (headers) {
          for (const k in headers) xhr.setRequestHeader(k, headers[k]);
        }
        xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
        xhr.onerror = () => resolve({ status: -1, text: "xhr network error" });
        xhr.send(body);
      } catch (e) {
        resolve({ status: -2, text: String(e) });
      }
    });
  }

  // Diagnostic: probe candidate field names once, then reuse the one that
  // returns a usable uri. A bare `file` field came back code:1053 无效图片数据
  // with all image fields empty (server didn't read a file under that key).
  let _uploadField = null;

  async function uploadOne(item) {
    const b64len = item && item.b64 ? item.b64.length : 0;
    const blob = b64ToBlob(item.b64, item.mime);
    const ext = item.mime === "image/png" ? "png" : "jpg";
    const fname = "image." + ext;
    const candidates = _uploadField ? [_uploadField] : ["file", "upfile", "image", "media"];
    let last = { httpStatus: 0, json: null, raw: "", field: null, b64len: b64len };
    for (let fi = 0; fi < candidates.length; fi++) {
      const field = candidates[fi];
      const fd = new FormData();
      // Real filename + extension; no explicit content-type so the browser
      // sets the multipart boundary.
      fd.append(field, blob, fname);
      const res = await xhrPost(arg.uploadUrl, fd, null);
      let json = null;
      try {
        json = JSON.parse(res.text);
      } catch (_) {}
      const d = (json && (json.data || json)) || {};
      const uri = d.uri || d.web_uri || d.origin_web_uri || "";
      last = {
        httpStatus: res.status,
        json: json,
        raw: (res.text || "").slice(0, 1200),
        field: field,
        b64len: b64len,
      };
      if (res.status === 200 && uri) {
        _uploadField = field;
        return last;
      }
    }
    return last;
  }

  function pickImageFields(up) {
    const j = up.json || {};
    const d = j.data || j;
    const uri = d.uri || d.web_uri || d.origin_web_uri || "";
    const url = d.url || d.web_url || "";
    const w = d.thumb_width || d.width || 0;
    const h = d.thumb_height || d.height || 0;
    return { uri: uri, url: url, w: w, h: h };
  }

  try {
    const uploads = [];
    let index = 0;

    // 1) Cover (optional).
    if (arg.cover) {
      const up = await uploadOne(arg.cover);
      uploads.push(up.json !== null ? up.json : up.raw);
      const f = pickImageFields(up);
      if (up.httpStatus !== 200 || !f.uri) {
        return {
          ok: false,
          step: "upload",
          index: index,
          httpStatus: up.httpStatus,
          raw: up.raw,
          field: up.field,
          b64len: up.b64len,
        };
      }
      arg.form.pgc_feed_covers = JSON.stringify([
        {
          id: 0,
          url: f.url,
          uri: f.uri,
          origin_uri: f.uri,
          thumb_width: f.w,
          thumb_height: f.h,
        },
      ]);
      index += 1;
    }

    // 2) Body images: upload + substitute __GEO_IMG_k__ -> <img src="uri">.
    const bodyImages = arg.bodyImages || [];
    for (let i = 0; i < bodyImages.length; i++) {
      const item = bodyImages[i];
      const up = await uploadOne(item);
      uploads.push(up.json !== null ? up.json : up.raw);
      const f = pickImageFields(up);
      if (up.httpStatus !== 200 || !f.uri) {
        return {
          ok: false,
          step: "upload",
          index: index,
          httpStatus: up.httpStatus,
          raw: up.raw,
          field: up.field,
          b64len: up.b64len,
        };
      }
      arg.form.content = arg.form.content
        .split(item.token)
        .join('<img src="' + f.uri + '">');
      index += 1;
    }

    // 3) Publish (form-urlencoded; save/entrance already set by Python).
    const body = new URLSearchParams(arg.form).toString();
    const res = await xhrPost(arg.publishUrl, body, {
      "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    });
    let data = null;
    try {
      data = JSON.parse(res.text);
    } catch (_) {}
    return {
      ok: true,
      step: "publish",
      uploads: uploads,
      publish: {
        httpStatus: res.status,
        data: data,
        raw: (res.text || "").slice(0, 1200),
      },
    };
  } catch (e) {
    return { ok: false, step: "upload", index: -1, httpStatus: -3, raw: String(e) };
  }
};
