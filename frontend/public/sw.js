// Service worker for Web Push notifications
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || "Feed Builder";
  const options = {
    body: data.body || "New articles available",
    icon: "/favicon.ico",
    badge: "/favicon.ico",
    data: { feedId: data.feedId },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const feedId = event.notification.data?.feedId;
  const url = feedId ? `/?feed=${feedId}` : "/";
  event.waitUntil(clients.openWindow(url));
});
