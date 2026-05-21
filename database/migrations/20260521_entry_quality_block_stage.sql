-- Allow entry-quality gates such as late-chase and low-RVOL blocks.

alter table if exists blocked_opportunities
    drop constraint if exists blocked_opportunities_block_stage_check;

alter table if exists blocked_opportunities
    add constraint blocked_opportunities_block_stage_check
    check (block_stage in (
        'gate',
        'ev',
        'ranking',
        'llm',
        'conviction',
        'price',
        'signal_consensus',
        'reward_risk',
        'exposure',
        'signal_alignment',
        'regime',
        'time',
        'sizing',
        'position',
        'entry_quality'
    ));
