class RuntimeEventBus {
	listeners = {};
	on(event, callback) {
		if (!this.listeners[event]) this.listeners[event] = [];
		this.listeners[event].push(callback);
		return () => {
			this.off(event, callback);
		};
	}
	off(event, callback) {
		if (!this.listeners[event]) return;
		this.listeners[event] = this.listeners[event].filter((fn) => fn !== callback);
		if (this.listeners[event].length === 0) {
			delete this.listeners[event];
		}
	}
	emit(event, data) {
		if (!this.listeners[event]) return;
		for (const listener of this.listeners[event]) {
			listener(data);
		}
	}
}
export const eventBus = new RuntimeEventBus();
