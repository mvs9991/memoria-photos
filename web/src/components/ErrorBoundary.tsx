import { Component, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/** A crash in one screen must not take the whole app down with a blank page. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: unknown) {
    console.error("UI error:", error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="state state-full">
        <AlertTriangle size={26} className="danger-text" />
        <h3>This screen hit a problem</h3>
        <p className="dim empty-hint">{this.state.error.message}</p>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-ghost" onClick={() => this.setState({ error: null })}>
            <RefreshCw size={15} /> Try again
          </button>
          <button className="btn btn-quiet" onClick={() => window.location.assign("/")}>
            Go home
          </button>
        </div>
        <p className="dim" style={{ fontSize: 12 }}>Your photos and library are unaffected.</p>
      </div>
    );
  }
}
