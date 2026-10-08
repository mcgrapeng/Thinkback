/** 全局 ErrorBoundary：包根路由，避免单组件抛错导致整页白屏。
 *
 * 不引入 react-error-boundary（一个 class 组件就够），production 可在 onError 接 sentry/logrocket。
 */

import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // ponytail: 上报到 sentry/logrocket 时挂在这里，先 console.error 占位
    console.error("[ErrorBoundary]", error, info);
  }

  private readonly handleReload = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-md p-6">
          <Alert variant="destructive">
            <AlertTriangle aria-hidden="true" />
            <AlertTitle>页面出现错误</AlertTitle>
            <AlertDescription>
              <p className="mb-3 break-words text-sm">
                {this.state.error.message.slice(0, 300) || "未知错误"}
              </p>
              <Button variant="outline" size="sm" onClick={this.handleReload}>
                <RefreshCw aria-hidden="true" /> 重试
              </Button>
            </AlertDescription>
          </Alert>
        </div>
      );
    }
    return this.props.children;
  }
}
