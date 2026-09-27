const byteFormatter = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 });
const sizeFormatter = new Intl.NumberFormat("zh-TW", {
	minimumFractionDigits: 2,
	maximumFractionDigits: 2
});
export function formatBytes(value, { exactBytes = false, fallback = "-" } = {}) {
	const bytes = typeof value === "number" ? value : Number(value);
	if (!Number.isFinite(bytes) || bytes < 0) return fallback;
	const roundedBytes = Math.round(bytes);
	if (roundedBytes < 1024) return `${byteFormatter.format(roundedBytes)} bytes`;
	const units = [
		"KiB",
		"MiB",
		"GiB",
		"TiB"
	];
	let amount = roundedBytes / 1024;
	let unitIndex = 0;
	while (amount >= 1024 && unitIndex < units.length - 1) {
		amount /= 1024;
		unitIndex += 1;
	}
	const readable = `${sizeFormatter.format(amount)} ${units[unitIndex]}`;
	return exactBytes ? `${readable}（${byteFormatter.format(roundedBytes)} bytes）` : readable;
}
export function formatGigabytes(value, { fallback = "-" } = {}) {
	const bytes = typeof value === "number" ? value : Number(value);
	if (!Number.isFinite(bytes) || bytes < 0) return fallback;
	return `${sizeFormatter.format(bytes / 1e9)} GB`;
}
export function formatProjectSize(value, { fallback = "-" } = {}) {
	const bytes = typeof value === "number" ? value : Number(value);
	if (!Number.isFinite(bytes) || bytes < 0) return fallback;
	const gigabyte = 1e9;
	if (bytes < gigabyte) {
		return `${sizeFormatter.format(bytes / 1e6)} MB`;
	}
	return `${sizeFormatter.format(bytes / gigabyte)} GB`;
}
export const format = {
	bytes: formatBytes,
	gigabytes: formatGigabytes,
	projectSize: formatProjectSize
};
