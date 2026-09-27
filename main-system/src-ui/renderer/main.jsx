import ReactDOM from "react-dom/client";
import App from "./ui/App.jsx";
import { HMRGuard } from "./shared/components/HMRGuard.jsx";
ReactDOM.createRoot(document.getElementById("root")).render(<HMRGuard>
    <App />
  </HMRGuard>);
