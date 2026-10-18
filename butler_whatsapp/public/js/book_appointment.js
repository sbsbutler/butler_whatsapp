// Butler WhatsApp - ERPNext online booking page (/book_appointment):
// tell the customer that the confirmation and reminder arrive on WhatsApp
// (Meta: the customer must know they will get WhatsApp messages from us).
(function () {
	if (!/^\/book_appointment\/?$/.test(location.pathname)) return;
	const add = () => {
		const input = document.getElementById("customer_number");
		if (!input || document.getElementById("butler-wa-booking-note")) return !!input;
		const ar = (document.documentElement.lang || "").toLowerCase().startsWith("ar");
		input.placeholder = ar ? "رقم الجوال (واتساب) مثل 05xxxxxxxx" : "Mobile (WhatsApp), e.g. +9665xxxxxxxx";
		const note = document.createElement("div");
		note.id = "butler-wa-booking-note";
		note.className = "small text-muted mt-1";
		note.textContent = ar
			? "💬 سنرسل تأكيد الموعد وتذكيراً به على واتساب إلى هذا الرقم."
			: "💬 We will send the appointment confirmation and a reminder to this number on WhatsApp.";
		input.insertAdjacentElement("afterend", note);
		return true;
	};
	if (!add()) document.addEventListener("DOMContentLoaded", add);
})();
