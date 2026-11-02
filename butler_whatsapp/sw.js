// Butler - service worker for push notifications (WhatsApp + calls from call_manager).
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
			requireInteraction: !!data.require_interaction,
			vibrate: data.vibrate || undefined,
			// open_type: which app handles the tap in an open Desk window (call_manager sends "butler_pbx_open")
			data: { url: data.url || "/desk/whatsapp-inbox", open_type: data.open_type || "butler_wa_open" },
		})
	);
});

self.addEventListener("notificationclick", (event) => {
	event.notification.close();
	const url = (event.notification.data && event.notification.data.url) || "/desk/whatsapp-inbox";
	const type = (event.notification.data && event.notification.data.open_type) || "butler_wa_open";
	event.waitUntil(
		self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
			for (const client of windows) {
				if (new URL(client.url).origin === self.location.origin && "focus" in client) {
					client.postMessage({ type, url });
					return client.focus();
				}
			}
			return self.clients.openWindow(url);
		})
	);
});
