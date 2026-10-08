// Butler - service worker for WhatsApp push notifications.
// Served by butler_whatsapp.push.service_worker with "Service-Worker-Allowed: /".
// No fetch handler on purpose: it never caches or intercepts site requests.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
	let data = {};
	try {
		data = event.data ? event.data.json() : {};
	} catch (e) {
		data = { title: "WhatsApp", body: event.data ? event.data.text() : "" };
	}
	const title = data.title || "WhatsApp";
	event.waitUntil(
		self.registration.showNotification(title, {
			body: data.body || "",
			tag: data.tag || "butler-wa",
			renotify: true,
			icon: "__ICON__",
			badge: "__ICON__",
			dir: "auto",
			data: { url: data.url || "/desk/whatsapp-inbox" },
		})
	);
});

self.addEventListener("notificationclick", (event) => {
	event.notification.close();
	const url = (event.notification.data && event.notification.data.url) || "/desk/whatsapp-inbox";
	event.waitUntil(
		self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
			for (const client of windows) {
				if (new URL(client.url).origin === self.location.origin && "focus" in client) {
					client.postMessage({ type: "butler_wa_open", url });
					return client.focus();
				}
			}
			return self.clients.openWindow(url);
		})
	);
});
