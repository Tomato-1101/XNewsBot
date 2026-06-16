// テーマ切替(localStorage 永続)と、破壊的/不可逆な操作の確認ダイアログ。
(function () {
  "use strict";

  // --- テーマ切替 ---
  var root = document.documentElement;
  var btn = document.getElementById("theme-toggle");

  function applyIcon() {
    if (btn) btn.textContent = root.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
  }
  applyIcon();

  if (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("xnews-theme", next); } catch (e) {}
      applyIcon();
    });
  }

  // --- 確認ダイアログ(誤操作・誤上書き防止) ---
  document.querySelectorAll("form.needs-confirm").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var msg = form.getAttribute("data-confirm") || "実行しますか？";
      var input = form.querySelector('input[type="time"]');
      if (input && input.value) msg += "\n\n新しい時刻: " + input.value;
      if (!window.confirm(msg)) e.preventDefault();
    });
  });
})();
