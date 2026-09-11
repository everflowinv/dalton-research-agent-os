import { WebSearchBroker } from "./broker.mjs";
import { BrokerServer } from "./server.mjs";

export function createPluginDefinition() {
  return {
    id: "dalton-openclaw-web-search-broker",
    name: "Dalton OpenClaw Web Search Broker",
    description: "Host-owned web search bridge for an external Dalton runtime.",
    register(api) {
      let server;
      api.registerService({
        id: "dalton-openclaw-web-search-broker",
        async start(ctx) {
          const broker = new WebSearchBroker(api.runtime, api.pluginConfig ?? {}, { hostConfig: api.config });
          server = new BrokerServer(broker);
          await server.start(ctx.stateDir);
          // Never log the query, the results or the provider credential.
          ctx.logger.info("Dalton web search broker started", {
            socketName: broker.config.socketName,
            legacyExpectedProvider: broker.config.expectedProvider,
            maxCount: broker.config.maxCount,
          });
        },
        async stop(ctx) {
          await server?.stop();
          server = undefined;
          ctx.logger.info("Dalton web search broker stopped");
        },
      });
    },
  };
}
