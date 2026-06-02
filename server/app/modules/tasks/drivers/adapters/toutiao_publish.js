// server/app/modules/tasks/drivers/adapters/toutiao_publish.js
// Runs inside the live Toutiao editor page via page.evaluate(js, arg).
// arg = { form: { <field>: <value>, ... } }
// Uses XMLHttpRequest so the page's global request hook (acrawler/secsdk)
// auto-appends a_bogus / msToken / _signature / x-secsdk-csrf-token.
// Returns { httpStatus, data, raw }.
async (arg) => {
  const url =
    "https://mp.toutiao.com/mp/agw/article/publish" +
    "?source=mp&type=article&aid=1231&mp_publish_ab_val=0";
  const body = new URLSearchParams(arg.form).toString();
  const res = await new Promise((resolve) => {
    try {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader(
        "content-type",
        "application/x-www-form-urlencoded;charset=UTF-8"
      );
      xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
      xhr.onerror = () => resolve({ status: -1, text: "xhr network error" });
      xhr.send(body);
    } catch (e) {
      resolve({ status: -2, text: String(e) });
    }
  });
  let data = null;
  try {
    data = JSON.parse(res.text);
  } catch (_) {}
  return { httpStatus: res.status, data: data, raw: (res.text || "").slice(0, 1200) };
};
