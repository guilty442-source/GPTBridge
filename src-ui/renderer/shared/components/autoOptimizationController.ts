import { ConnectionTester } from './connectionTester';
import { AiValidator, SchemaNode } from './aiValidator';

/**
 * 自動優化流程控制器
 * 負責管理「啟動」與「終止」邏輯
 */
export class AutoOptimizationController {
  private isRunning: boolean = false;
  private abortController: AbortController | null = null;

  private readonly outlineSchema: Record<string, SchemaNode> = {
    toolName: { type: "string" },
    description: { type: "string" },
    steps: { 
      type: "array", 
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          action: { type: "string" },
          description: { type: "string" },
          params: { type: "object" }
        }
      }
    }
  };

  /**
   * 泛型重試包裝器：將重試、終止與延遲邏輯抽離，保持主流程乾淨
   */
  private async executeWithRetry<T>(
    operationName: string,
    maxRetries: number,
    signal: AbortSignal,
    log: (msg: string) => void,
    fn: () => Promise<T>
  ): Promise<T> {
    let retryCount = 0;
    while (true) {
      try {
        if (signal.aborted) throw Object.assign(new Error('Aborted'), { name: 'AbortError' });
        return await fn();
      } catch (e: any) {
        if (e.name === 'AbortError' || signal.aborted) {
          throw Object.assign(new Error('Aborted'), { name: 'AbortError' });
        }
        if (retryCount >= maxRetries) {
          log(`[CRITICAL] [重試耗盡] ${operationName}失敗: ${e.message}`);
          throw e;
        }
        retryCount++;
        log(`[重試 ${retryCount}/${maxRetries}] ${operationName}發生錯誤，等待後重新嘗試...`);
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
    }
  }

  /**
   * 啟動自動優化 (一鍵啟動)
   */
  async start(context: any, updateUI: (msg: string, progress?: number) => void, log: (msg: string) => void) {
    if (this.isRunning) return;
    
    this.isRunning = true;
    this.abortController = new AbortController();
    const signal = this.abortController.signal;

    try {
      log("--- 自動優化流程開始 ---");

      updateUI("環境自檢中...", 10);
      const health = await ConnectionTester.checkOverallHealth(context);
      if (!health.ok) throw new Error(health.summary);
      log(`[連線成功] ${health.summary}`);

      // 2. Gemini 分析階段 (需求 5, 6, 7)
      const geminiData = await this.runPhase(
        'Gemini 分析',
        {
          provider: 'gemini',
          command: 'ANALYZE_OUTLINE',
          payload: { prompt: `### ROLE: SENIOR TOOL AUDITOR\n### TASK: 深度審計並提供重構建議\n\n${context.parentToolOutline}`, config: context.config }
        },
        context, updateUI, log, signal, 20
      );
      context.updateFlowStatus('GEMINI_ANALYSIS_COMPLETED', geminiData);

      // 3. GPT 統合階段 (需求 8, 9, 10)
      if (signal.aborted) return;
      const finalResult = await this.runPhaseWithRetry(
        'GPT 統合',
        {
          provider: 'gpt',
          command: 'CONSOLIDATE_ANALYSIS',
          payload: { prompt: `### ROLE: PRINCIPAL SYSTEM ARCHITECT\n### TASK: 重構 JSON 大綱\n\nORIGINAL: ${context.parentToolOutline}\nAUDIT: ${JSON.stringify(geminiData)}`, config: context.config }
        },
        context, updateUI, log, signal, 60
      );

      // 最終結果驗證與退避
      let validatedResult = finalResult;
      if (!finalResult && !signal.aborted) {
        log("觸發退避機制：嘗試使用 Gemini 進行最終統合...");
        validatedResult = await this.runPhase('Gemini 退避統合', {
          provider: 'gemini',
          command: 'CONSOLIDATE_ANALYSIS',
          payload: { prompt: `請整合建議產出最終 JSON: ${JSON.stringify(geminiData)}`, config: context.config }
        }, context, updateUI, log, signal, 80);
      }

      if (validatedResult) {
        const validation = AiValidator.validate(validatedResult, this.outlineSchema);
        if (!validation.isValid) log(`[警告] 優化大綱校驗異常: ${validation.errors.join("; ")}`);

        context.updateFlowStatus('OPTIMIZATION_REPORT', validatedResult);
      context.updateFlowStatus('COMPLETED', finalResult);
        updateUI("優化成功", 100);
      }

    } catch (error: any) {
      if (error.name === 'AbortError') {
        log("優化流程已手動終止");
        updateUI("已終止", 0);
      } else {
        log(`[終端錯誤] ${error.message}`);
        updateUI("AI 分析失敗", 0);
      }
    } finally {
      this.isRunning = false;
      this.abortController = null;
    }
  }

  private async runPhase(name: string, params: any, context: any, updateUI: any, log: any, signal: AbortSignal, progress: number) {
    log(`${name}開始`);
    updateUI(`${name}進行中...`, progress);
    const res = await context.aiDispatch({ ...params, is_volatile: true, signal });
    if (!res?.content) throw new Error(`${name}回傳內容為空`);
    const data = AiValidator.parse(res.content);
    log(`${name}完成`);
    return data;
  }

  private async runPhaseWithRetry(name: string, params: any, context: any, updateUI: any, log: any, signal: AbortSignal, progress: number) {
    let lastError;
    for (let i = 0; i < 3; i++) {
      try {
        return await this.runPhase(name, params, context, updateUI, log, signal, progress + (i * 5));
      } catch (e) {
        lastError = e;
        log(`[重試 ${i+1}/3] ${name}失敗`);
      }
    }
    return null; // 交給退避機制處理
  }

  /**
   * 終止優化流程
   */
  terminate() {
    if (this.abortController) {
      this.abortController.abort();
    }
  }

  get status() {
    return this.isRunning ? 'RUNNING' : 'IDLE';
  }
}
