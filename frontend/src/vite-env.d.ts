/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the deployed SentinelGridKnowledgeQuery API, e.g.
   * "https://m1uqr1hsq3.execute-api.ap-south-1.amazonaws.com". No
   * trailing slash. See .env.example. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
