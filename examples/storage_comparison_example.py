from __future__ import annotations

import argparse
from pathlib import Path

from discoolpy import (
    load_yaml_config,
    output_path,
    make_riyadh_weather_and_load_profiles,
    run_configured_case,
    write_storage_comparison_summary,
    make_storage_comparison_plot,
)

DEFAULT_CONFIG = Path('../configs/riyadh_three_building_length_derived_pr.yaml')


def run_storage_comparison(config_path: str | Path = DEFAULT_CONFIG) -> None:
    """Run paired three-building examples with and without optional cold storage.

    All design values, pipe parameters, storage settings, synthetic profile
    controls, solver settings, and output paths are read from a YAML scenario
    file. The default configuration uses known pipe lengths and pipe geometry,
    derives robust Darcy-Weisbach design pressure ratios, and then runs the
    transient TESPy comparison without hard-coded example inputs.
    """
    config = load_yaml_config(config_path)
    profile_df = make_riyadh_weather_and_load_profiles(config)
    profile_df.to_csv(output_path(config, 'profile_csv', 'storage_comparison_input_profile.csv'), index=False)

    no_storage = run_configured_case(config, profile_df, 'without_storage', use_storage=False)
    no_storage.to_csv(output_path(config, 'without_storage_csv', 'without_storage_results.csv'), index=False)

    with_storage = run_configured_case(config, profile_df, 'with_storage', use_storage=True)
    with_storage.to_csv(output_path(config, 'with_storage_csv', 'with_storage_results.csv'), index=False)

    write_storage_comparison_summary(config, no_storage, with_storage)
    make_storage_comparison_plot(config, no_storage, with_storage)
    print(f"Summary written to {output_path(config, 'summary_md', 'storage_comparison_summary.md')}")


def main() -> None:
    parser = argparse.ArgumentParser(description='Run the YAML-driven district-cooling storage comparison example.')
    parser.add_argument(
        '--config',
        default=str(DEFAULT_CONFIG),
        help='Path to the YAML scenario file. Defaults to the length-based Riyadh three-building scenario.',
    )
    args = parser.parse_args()
    run_storage_comparison(args.config)


if __name__ == '__main__':
    main()
