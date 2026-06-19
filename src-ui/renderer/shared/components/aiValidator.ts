/**
 * AI 回傳結果驗證工具
 */

export type SchemaType = "string" | "number" | "boolean" | "object" | "array";
export interface SchemaNode {
  type: SchemaType;
  properties?: Record<string, SchemaNode>;
  items?: SchemaNode;
}

export class AiValidator {
  /**
   * 從原始字串中提取 JSON 並解析
   */
  static parse<T>(rawText: string): T {
    try {
      let cleanText = rawText.trim();
      // 移除 Markdown 的程式碼區塊標記
      if (cleanText.startsWith("```")) {
        cleanText = cleanText.replace(/^```(?:json)?|```$/g, "").trim();
      }
      const jsonMatch = cleanText.match(/\{[\s\S]*\}|\[[\s\S]*\]/);
      if (!jsonMatch) {
        throw new Error("無法在回傳內容中找到有效的 JSON 結構");
      }
      return JSON.parse(jsonMatch[0]) as T;
    } catch (error) {
      throw new Error(`JSON 解析失敗: ${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  /**
   * 驗證物件是否符合定義的 Schema
   */
  static validate(data: any, schema: Record<string, SchemaNode>): { isValid: boolean; errors: string[] } {
    const errors: string[] = [];
    if (!data || typeof data !== "object") {
      return { isValid: false, errors: ["根節點無效：解析結果非有效的 JSON 物件"] };
    }
    const validateNode = (value: any, node: SchemaNode, path: string) => {
      const { type, properties, items } = node;
      const actualType = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
      let isValid = false;

      if (type === "array") {
        isValid = actualType === "array";
        if (isValid && items) {
          for (let i = 0; i < value.length; i++) validateNode(value[i], items, `${path}[${i}]`);
        }
      } else if (type === "object") {
        isValid = actualType === "object";
        if (isValid && properties) {
          for (const key in properties) validateNode(value ? (value as any)[key] : undefined, properties[key], path ? `${path}.${key}` : key);
        }
      } else {
        isValid = actualType === type;
      }
      if (!isValid) errors.push(`[欄位錯誤] ${path || "root"}: 預期為 ${type} 但收到 ${actualType}`);
    };
    for (const key in schema) validateNode(data[key], schema[key], key);
    return { isValid: errors.length === 0, errors };
  }
}