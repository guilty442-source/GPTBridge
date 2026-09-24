/*
 * 星澄 AI 投資管理與自動操盤系統 — risk core (C).
 *
 * Fail-closed order limit evaluation. Mirrors the Python reference
 * implementation in src/backend/services/investment_mobile/trading/
 * risk_engine.py — the same contract, decision-free.
 *
 * The Python façade loads this via ctypes when a build is present;
 * until then the reference implementation is authoritative.
 *
 * Contract: risk_evaluate_order() returns RISK_APPROVED only when every
 * configured limit passes. Any missing limit, zero quantity, or missing
 * notional is a rejection.
 */

#include <stddef.h>

#define RISK_APPROVED 0
#define RISK_REJECTED_MARKET 1
#define RISK_REJECTED_QUANTITY 2
#define RISK_REJECTED_NO_PRICE 3
#define RISK_REJECTED_ORDER_NOTIONAL 4
#define RISK_REJECTED_DAILY_ORDERS 5
#define RISK_REJECTED_DAILY_LOSS 6
#define RISK_REJECTED_POSITION_NOTIONAL 7
#define RISK_REJECTED_WEIGHT 8

typedef struct {
    double max_order_notional;
    double max_position_notional;
    double max_daily_loss;
    int    max_orders_per_day;
    double max_single_position_weight;
    int    require_price;
    /* allowed markets bitmask: bit0=tw bit1=us bit2=fund */
    unsigned int allowed_market_mask;
} RiskLimits;

typedef struct {
    unsigned int market_bit;   /* 1<<0 tw, 1<<1 us, 1<<2 fund */
    int          side;         /* 1 = buy, -1 = sell          */
    double       quantity;
    double       notional;
    double       existing_position_notional;
    double       existing_position_value;
    double       total_portfolio_value;
    int          daily_order_count;
    double       daily_realized_pnl;
} RiskOrderInput;

int risk_evaluate_order(const RiskLimits *limits, const RiskOrderInput *order)
{
    double projected;

    if (limits == NULL || order == NULL)
        return RISK_REJECTED_QUANTITY; /* fail closed */

    if ((order->market_bit & limits->allowed_market_mask) == 0)
        return RISK_REJECTED_MARKET;

    if (order->quantity <= 0.0)
        return RISK_REJECTED_QUANTITY;

    if (limits->require_price && order->notional <= 0.0)
        return RISK_REJECTED_NO_PRICE;

    if (limits->max_order_notional <= 0.0)
        return RISK_REJECTED_ORDER_NOTIONAL; /* missing limit fails closed */
    if (order->notional > limits->max_order_notional)
        return RISK_REJECTED_ORDER_NOTIONAL;

    if (limits->max_orders_per_day <= 0 ||
        order->daily_order_count >= limits->max_orders_per_day)
        return RISK_REJECTED_DAILY_ORDERS;

    if (limits->max_daily_loss <= 0.0 ||
        order->daily_realized_pnl <= -limits->max_daily_loss)
        return RISK_REJECTED_DAILY_LOSS;

    if (limits->max_position_notional <= 0.0)
        return RISK_REJECTED_POSITION_NOTIONAL;
    projected = order->existing_position_notional +
                order->notional * (double)order->side;
    if (projected > limits->max_position_notional)
        return RISK_REJECTED_POSITION_NOTIONAL;

    if (limits->max_single_position_weight > 0.0 &&
        order->total_portfolio_value > 0.0) {
        double projected_value = order->existing_position_value +
                                 order->notional * (double)order->side;
        if (projected_value / order->total_portfolio_value >
            limits->max_single_position_weight)
            return RISK_REJECTED_WEIGHT;
    }

    return RISK_APPROVED;
}
