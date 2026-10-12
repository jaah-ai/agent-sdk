// Typings for @jaah/agent-sdk. The runtime is ./index.js (plain ESM, no build step).

export type LogLevel = "debug" | "info" | "warning" | "error";
export declare const LOG_LEVELS: readonly LogLevel[];

export interface TaskInfo {
  id: number;
  title: string;
  body: string;
  persona: string;
  autonomous: boolean;
  out_of_scope: string;
}

export interface ProjectInfo {
  name: string;
  repo?: string | null;
  integration_branch?: string | null;
  branch_prefix?: string | null;
  gate_quick_cmd?: string | null;
  gate_full_cmd?: string | null;
  push_enabled?: boolean;
  has_repo?: boolean;
  [key: string]: unknown;
}

export interface AgentInfo {
  id: string;
  display_name: string;
  version: string;
  commit: string;
  [key: string]: unknown;
}

export interface InputDocument {
  kind: string;
  name: string;
  path: string;
}

export interface TaskContext {
  task: TaskInfo;
  project: ProjectInfo;
  agent: AgentInfo;
  security_gate: "human" | "agent";
  /** env JAAH_WORKTREE: the task's checkout, also the process's cwd. */
  worktree: string | null;
  /** From the file at env JAAH_AGENT_INPUTS. */
  inputs: InputDocument[];
}

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface Question {
  question: string;
  header?: string;
  options: Array<QuestionOption | string>;
  multiSelect?: boolean;
  recommendedIndex?: number;
}

export interface Answer {
  status: "answered" | "dismissed";
  selections: string[];
  text: string;
}

export interface CredentialInfo {
  name: string;
  type: string;
  description: string;
  fields: string[];
  [key: string]: unknown;
}

export interface Credential {
  name: string;
  type: string;
  config: Record<string, unknown>;
  access_token?: string | null;
  access_token_expires_at?: string | null;
  [key: string]: unknown;
}

export interface SearchFilters {
  state?: string;
  persona?: string;
  hasArtifacts?: boolean;
  artifactKind?: "report" | "prd" | "architecture" | "file" | "attachment";
  limit?: number;
}

export type TerminalType = "merged_pr" | "no_pr" | (string & {});

export interface SettleOptions {
  prUrl?: string;
  noPrReason?: "duplicate" | "already_shipped" | "no_code_needed" | "remediated" | (string & {});
  noPrEvidence?: string;
  /** Any other settle field (e.g. reconMap), sent as snake_case. */
  [gate: string]: unknown;
}

export interface SettleResult {
  stored?: boolean;
  settled?: boolean;
  state?: string;
  [key: string]: unknown;
}

export declare class JaahError extends Error {}
export declare class JaahUnavailable extends JaahError {}
export declare class JaahRefused extends JaahError {
  constructor(reason: string, detail?: string);
  readonly reason: string;
  readonly detail: string;
}
export declare class NotARunner extends JaahError {}

export declare function connect(options?: { socketPath?: string; eventsPath?: string }): Promise<Agent>;

export declare class Agent {
  private constructor(wire: unknown, events: unknown, options?: unknown);
  task(): Promise<TaskContext>;
  log(message: string, level?: LogLevel, data?: Record<string, unknown>): void;
  reportUsage(model: string, inputTokens: number, outputTokens: number, cacheRead?: number, cacheWrite?: number): void;
  notify(message: string): Promise<unknown>;
  ask(questions: Question[], options?: { pollInterval?: number; timeout?: number | null }): Promise<Answer[]>;
  credentials(): Promise<CredentialInfo[]>;
  credential(name: string): Promise<Credential>;
  artifact(kind: "report" | "prd" | "architecture" | "file", path: string): Promise<unknown>;
  readTask(taskId: number): Promise<unknown>;
  searchTasks(q: string, filters?: SearchFilters): Promise<{ tasks: unknown[]; truncated: boolean }>;
  settle(summary: string, terminalType: TerminalType, options?: SettleOptions): Promise<SettleResult>;
  fail(reason: string): Promise<unknown>;
}
