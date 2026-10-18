// يرفع كل ملف في طلب مستقل، فلا يتقيد العدد الكلي بحد حجم الطلب الواحد
(function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!confirm(f.dataset.confirm)) e.preventDefault();
    });
  });

  var form = document.querySelector("form[data-upload]");
  if (!form) return;
  var filesInput = form.querySelector("#pf-files");
  var folderInput = form.querySelector("#pf-folder");
  var list = form.querySelector(".up-list");
  var count = form.querySelector(".up-count");
  var button = form.querySelector("button[type=submit]");
  var max = Number(form.dataset.max);
  var allowed = form.dataset.exts.split(",").map(function (s) { return s.trim(); });
  var picked = [];

  function relPath(file) { return file.webkitRelativePath || file.name; }

  function refresh() {
    picked = [].concat(Array.from(filesInput.files), Array.from(folderInput.files));
    count.textContent = picked.length ? "تم اختيار " + picked.length + " ملف" : "";
  }
  filesInput.addEventListener("change", refresh);
  folderInput.addEventListener("change", refresh);

  function row(text, cls) {
    var li = document.createElement("li");
    li.textContent = text;
    if (cls) li.className = cls;
    list.appendChild(li);
    return li;
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    refresh();
    if (!picked.length) { count.textContent = "اختر ملفاً أو مجلداً أولاً"; return; }
    button.disabled = true;
    list.innerHTML = "";
    var ok = 0, fail = 0;
    for (var i = 0; i < picked.length; i++) {
      var file = picked[i], path = relPath(file);
      var ext = path.indexOf(".") > -1 ? path.split(".").pop().toLowerCase() : "";
      var li = row(path + " … جارٍ الرفع");
      count.textContent = "جارٍ رفع " + (i + 1) + " من " + picked.length;
      if (allowed.indexOf(ext) === -1) { li.textContent = path + " — نوع غير مسموح"; li.className = "err"; fail++; continue; }
      if (file.size > max) { li.textContent = path + " — أكبر من الحد المسموح"; li.className = "err"; fail++; continue; }
      var fd = new FormData();
      fd.append("csrf", form.elements.csrf.value);
      fd.append("relpath", path);
      fd.append("files", file, file.name);
      try {
        var r = await fetch(form.action, { method: "POST", body: fd, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" });
        var j = await r.json().catch(function () { return {}; });
        if (r.ok && j.ok) { li.textContent = path + " — تم الرفع"; li.className = "ok"; ok++; }
        else { li.textContent = path + " — " + (j.error || "فشل الرفع"); li.className = "err"; fail++; }
      } catch (err) {
        li.textContent = path + " — تعذّر الاتصال"; li.className = "err"; fail++;
      }
    }
    count.textContent = "تم رفع " + ok + " ملف" + (fail ? "، وفشل " + fail : "");
    button.disabled = false;
    if (ok) setTimeout(function () { location.reload(); }, fail ? 3000 : 700);
  });
})();
