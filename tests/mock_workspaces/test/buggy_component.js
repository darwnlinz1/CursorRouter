// Fixed component after Auto-Fix Loop
function calculateArcadeScore(baseScore, multiplier) {
    const total = baseScore * multiplier;
    if (total > 1000) {
        return total * 1.5;
    }
    return total;
}
module.exports = { calculateArcadeScore };
