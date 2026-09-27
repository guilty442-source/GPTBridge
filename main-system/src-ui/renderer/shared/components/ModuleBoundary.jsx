import { Component } from "react";
import { mainSystemLocale } from "@/locales/main-system";
const mb = mainSystemLocale.moduleBoundary;
/**
* Module fault isolation: a render error inside one module is contained to
* that module and never blanks or stalls the rest of the UI.
*/
export class ModuleBoundary extends Component {
	state = { failed: false };
	static getDerivedStateFromError() {
		return { failed: true };
	}
	componentDidCatch(error, info) {
		console.error(`[module-boundary] ${this.props.name} failed`, error, info);
	}
	render() {
		if (this.state.failed) {
			return <div className="module-fault" role="status" data-testid={`module-fault-${this.props.name}`}>
          <strong>{this.props.name}</strong>
          <span>{mb.fallbackMessage}</span>
        </div>;
		}
		return this.props.children;
	}
}
