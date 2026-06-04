// Service worker for Web Push — shows the "session starting" notification even
// when the tab is backgrounded (and, on installed iOS PWAs, when closed).
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data.json(); } catch (e) {}
  var title = data.title || "Breakouts";
  var opts = {
    body: data.body || "",
    tag: data.tag || "breakouts",
    data: { url: data.url || "/published" },
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/published";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (wins) {
      for (var i = 0; i < wins.length; i++) {
        if ("focus" in wins[i]) { wins[i].navigate(url); return wins[i].focus(); }
      }
      return clients.openWindow(url);
    })
  );
});
