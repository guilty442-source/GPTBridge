// 星澄 AI 投資管理與自動操盤系統 — strategy engine (C++).
//
// Formal model inference and strategy evaluation surface. The Python
// StrategyEngine (src/.../trading/strategy_engine.py) registers strategies
// and owns the signal book; this component provides the high-throughput
// evaluation path for computationally heavy strategies and, later, formal
// model inference (ONNX/native) loaded through a governed artifact path.
//
// Decision-free contract: evaluate() never touches markets, networks or
// persistence — it maps (signal, strategy params) -> order intent fields.

#include <cstdint>
#include <string>
#include <vector>

namespace star_trading {

struct SignalView {
    std::string instrument;
    std::string market;
    std::string side;
    double confidence = 0.0;
    double price = 0.0;
    double quantity = 0.0;
    std::string signal_id;
};

struct IntentView {
    bool emit = false;             // no intent -> signal stays advisory
    std::string instrument;
    std::string market;
    std::string side;
    double quantity = 0.0;
    double price = 0.0;
    double notional = 0.0;
    std::string strategy_id;
    std::string signal_id;
};

struct StrategyParams {
    std::string strategy_id;
    double min_confidence = 0.5;   // fail-closed below this
    double max_quantity = 0.0;     // 0 -> no cap at this layer (risk engine caps)
};

class StrategyEngine {
public:
    void registerStrategy(StrategyParams params) {
        strategies_.push_back(std::move(params));
    }

    std::vector<IntentView> evaluate(const SignalView& signal) const {
        std::vector<IntentView> out;
        for (const auto& s : strategies_) {
            if (signal.confidence < s.min_confidence || signal.quantity <= 0.0)
                continue;
            IntentView intent;
            intent.emit = true;
            intent.instrument = signal.instrument;
            intent.market = signal.market;
            intent.side = signal.side;
            intent.quantity =
                (s.max_quantity > 0.0 && signal.quantity > s.max_quantity)
                    ? s.max_quantity
                    : signal.quantity;
            intent.price = signal.price;
            intent.notional = intent.quantity * intent.price;
            intent.strategy_id = s.strategy_id;
            intent.signal_id = signal.signal_id;
            out.push_back(std::move(intent));
        }
        return out;
    }

private:
    std::vector<StrategyParams> strategies_;
};

}  // namespace star_trading

// C ABI for the Python façade (ctypes) — same contract as evaluate().
extern "C" {

__declspec(dllexport) void* strategy_engine_create() {
    return new star_trading::StrategyEngine();
}

__declspec(dllexport) void strategy_engine_destroy(void* engine) {
    delete static_cast<star_trading::StrategyEngine*>(engine);
}

}  // extern "C"
