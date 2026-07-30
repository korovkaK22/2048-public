# docs/knowledge — вивантажена агентська пам'ять (snapshot 2026-07-29)

Це **дамп асистентської пам'яті** (`~/.claude/projects/.../memory/`), яка досі жила ПОЗА git
і тому зникла б разом із машиною. Останній пуш проєкту — тому все, що там накопичилось за
2026-07-11 … 2026-07-25, лежить тут **дослівно, без переказу**: кожен файл = один факт/висновок,
у своєму оригінальному вигляді (frontmatter + тіло, посилання `[[name]]` між файлами).

Чому окрема тека, а не `experiment-log.md`: лог експериментів — це хронологія «config → результат»;
ці файли — витягнуті з неї **висновки й операційні правила**, які переживають окремі прогони.
`_index-from-memory.md` — оригінальний індекс пам'яті (по одному рядку на факт).

**Читати як історичний snapshot, не як актуальну документацію.** Актуальні контракти й пайплайн —
`docs/architecture.md`; операційні граблі — `docs/ops-playbook.md`; хронологія — `docs/experiment-log.md`.
Якщо файл згадує конкретний скрипт / прапорець / тулзу — перевір, що воно ще існує, перш ніж на це спиратись.

## Що де лежить

**Головний результат — стеля скору (чому «побити 58k» закрито):**
- [beat-58k-final-closure.md](beat-58k-final-closure.md) — **фінальне закриття** (2026-07-25): banker-ON хвіст n=8220 → стіна 51.1k, гіпотеза «58k = кумулятивна сума» вбита логічно, симуляційних левериджів не лишилось.
- [beat-58k-via-k4-cascade.md](beat-58k-via-k4-cascade.md) — 26KB повної історії полювання: 10 ортогональних доказів стелі (R4 EV-gate, DP max-P tablebase, coop-probe, ретрейни mean/mass/expectile, highvar-β, farm-MAP-Elites, κ-tilt, goal-beam, CMDP). **Список закритих глухих кутів — читати ПЕРЕД будь-якою новою ідеєю.**
- [theoretical-max-score.md](theoretical-max-score.md) — реальна межа: 106,496 за легальністю / ~81k за досяжністю (51k і 100k були обидва неправильні).
- [record-is-midgame-mass.md](record-is-midgame-mass.md) — рекорд доведено досяжним у симі: «wall»-ретрейн банкував 50284 і 50860 через solver-cashout там, де чемпіон був 0/240.

**Стратегія і налаштування бота:**
- [cashout-and-champion-state.md](cashout-and-champion-state.md) — cash-out +2048 підтверджено живцем; чемпіон = tail-net λ=0.003; рекорди 49.6k/49.4k.
- [banker-arm-timing-is-tuned.md](banker-arm-timing-is-tuned.md) — тайминг банкіра **вже підібраний**: зниження target/arm убиває топ-3 (9/10 сідів −1.3…−5.1k). Не чіпати.
- [soft-not-hard-overrides.md](soft-not-hard-overrides.md) — м'які leaf-пенальті допомагають, жорсткі veto/risk-backup шкодять (доведено 3×).

**Операційна дисципліна (граблі):**
- [2048-simulation-discipline.md](2048-simulation-discipline.md) — один game-runner за раз, ніколи глобальний `pkill`, wall-clock бреше під навантаженням, n=12 — це не дані, судити по обрізаному хвосту.
- [mac-max-power-commands.md](mac-max-power-commands.md) — `caffeinate` + `pmset` проти тротлінгу на важких симах; як відкотити.
- [games-continue-infra-is-fine.md](games-continue-infra-is-fine.md) — живі ігри НЕ падали: `vision_mismatch`/`screen_*` — артефакти логування/нарізки ігор, результати валідні.
- [numbers-must-come-from-data.md](numbers-must-come-from-data.md) — чотири випадки, коли число, виведене міркуванням, виявилося хибним, і що сказали дані.
- [poc-then-optimize.md](poc-then-optimize.md) — спершу PoC ≤20 хв (бінарне «стіна зрушила?»), повна оптимізація n≥400 лише після підтвердженого шансу.
