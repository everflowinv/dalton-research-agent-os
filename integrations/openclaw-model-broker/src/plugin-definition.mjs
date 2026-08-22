import { ModelBroker } from "./broker.mjs";
import { BrokerServer } from "./server.mjs";

export function createPluginDefinition() {
  return {
    id: "dalton-openclaw-model-broker",
    name: "Dalton OpenClaw Model Broker",
    description: "Host-owned completion bridge for an external Dalton runtime.",
    register(api) {
      let server;
      api.registerService({
        id: "dalton-openclaw-model-broker",
        async start(ctx) {
          const broker = new ModelBroker(api.runtime, api.pluginConfig ?? {});
          server = new BrokerServer(broker);
          await server.start(ctx.stateDir);
          const capability = api.runtime?.llm?.capabilities?.providerControls;
          ctx.logger.info("Dalton model broker started", {
            socketName: broker.config.socketName,
            profileCount: broker.config.profiles.size,
            providerControlCapability: capability
              ? { version: capability.version, modes: capability.modes, transports: capability.transports ?? capability.transport ?? null }
              : null,
          });
        },
        async stop(ctx) {
          await server?.stop();
          server = undefined;
          ctx.logger.info("Dalton model broker stopped");
        },
      });
    },
  };
}
