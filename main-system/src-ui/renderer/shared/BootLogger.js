export const BootLogger = { log: (service, event, detail = {}, level = "info") => {
	const entry = {
		timestamp: new Date().toISOString(),
		service,
		event,
		detail,
		level
	};
	console.log(`[${entry.service}] ${entry.event}`, entry);
} };
