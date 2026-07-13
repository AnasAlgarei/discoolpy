from tespy.connections import Connection
from tespy.components import (
    Condenser, CycleCloser, Compressor, HeatExchanger, Subsystem, Valve,
)
import csv
import json
from typing import Dict, Optional, Any, List


class Chiller(Subsystem):
    """
    Enhanced Chiller class with comprehensive attributes and generic CSV import capability.
    
    This class extends the basic TESPy Subsystem to represent a vapor compression
    chiller with detailed specifications including performance data, physical
    characteristics, and operating parameters. Supports importing performance
    data from any certification standard CSV files.
    
    Parameters
    ----------
    label : str
        Label for the chiller component
    T_evap : float, optional
        Evaporating temperature in °C (default: 2)
    T_cond : float, optional
        Condensing temperature in °C (default: 40)
    eta_s : float, optional
        Compressor isentropic efficiency (default: 0.75)
    Q_evap : float, optional
        Evaporator cooling capacity in W (default: 50000, i.e. 50 kW).
        This is specified as a positive value; internally it is applied
        with a negative sign per TESPy convention.
    refrigerant : str, optional
        Refrigerant type (default: 'R134a')
    pr_evap_1 : float, optional
        Evaporator pressure ratio, hot side (default: 1.0)
    pr_evap_2 : float, optional
        Evaporator pressure ratio, cold side (default: 1.0)
    pr_cond_1 : float, optional
        Condenser pressure ratio, hot side (default: 1.0)
    pr_cond_2 : float, optional
        Condenser pressure ratio, cold side (default: 1.0)
    **kwargs : dict
        Optional chiller attributes (e.g., nominal_cooling_capacity, nominal_cop)
        
    Examples
    --------
    Basic usage:
    >>> chiller = Chiller('my_chiller')
    
    With attributes:
    >>> chiller = Chiller('my_chiller', nominal_cooling_capacity=500, nominal_cop=5.5)
    
    With CSV import:
    >>> chiller = Chiller('my_chiller')
    >>> chiller.import_from_csv('certification_data.csv')
    """
    
    def __init__(self, label, T_evap=2, T_cond=40, eta_s=0.75, Q_evap=50000, refrigerant='R134a',
                 pr_evap_1=1.0, pr_evap_2=1.0, pr_cond_1=1.0, pr_cond_2=1.0, **kwargs):
        self.num_in = 2
        self.num_out = 2
        
        # Store cycle parameters BEFORE calling super().__init__
        # because super().__init__ calls create_network()
        self._T_evap = T_evap
        self._T_cond = T_cond
        self._eta_s = eta_s
        self._Q_evap = Q_evap
        self._refrigerant = refrigerant
        self._pr_evap_1 = pr_evap_1
        self._pr_evap_2 = pr_evap_2
        self._pr_cond_1 = pr_cond_1
        self._pr_cond_2 = pr_cond_2
        self._evaporator = None
        self._condenser = None
        self._compressor = None
        self._expansion_valve = None
        self._internal_connections = {}
        
        # Initialize all attributes BEFORE calling super().__init__
        # because super().__init__ calls create_network()
        self._init_attributes()
        
        # Now call parent class __init__ which will call create_network()
        super().__init__(label)
        
        # Set any provided attributes from kwargs
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                print(f"Warning: Unknown attribute '{key}' ignored")
    
    def _init_attributes(self):
        """Initialize all chiller attributes with None as default."""
        
        # General Configuration Attributes
        self.model_id = None
        self.manufacturer = None
        self.model_name = None
        self.chiller_type = None  # 'water/water', 'air/water', 'brine/water', etc.
        self.compressor_type = None  # 'centrifugal', 'screw', 'scroll', 'reciprocating'
        self.year_manufactured = None
        
        # Capacity and Performance Attributes
        self.nominal_cooling_capacity = None  # kW
        self.nominal_heating_capacity = None  # kW
        self.min_capacity = None  # kW
        self.max_capacity = None  # kW
        self.nominal_cop = None  # Coefficient of Performance
        self.nominal_eer = None  # Energy Efficiency Ratio
        self.iplv = None  # Integrated Part Load Value
        self.nplv = None  # Non-standard Part Load Value
        self.scop = None  # Seasonal Coefficient of Performance
        self.seer = None  # Seasonal Energy Efficiency Ratio
        
        # Operating Conditions Attributes
        self.rated_evaporator_inlet_temp = None  # °C
        self.rated_evaporator_outlet_temp = None  # °C
        self.rated_condenser_inlet_temp = None  # °C
        self.rated_condenser_outlet_temp = None  # °C
        self.min_evaporator_temp = None  # °C
        self.max_condenser_temp = None  # °C
        self.min_operating_temp = None  # °C
        self.max_operating_temp = None  # °C
        
        #self._T_evap = None  # Evaporating temperature for cycle specification
        #self._T_cond = None # Condensing temperature for cycle specification
        #self._eta_s = None  # Isentropic efficiency for cycle specification
        #self._Q_evap = None  # Evaporator cooling capacity for cycle specification
        #self._refrigerant = None  # Refrigerant type for cycle specification
        #self._pr_evap_1 = None  # Evaporastor pressure ratio 1 for cycle specification
        #self._pr_evap_2 = None  # Evaporator pressure ratio 2 for cycle specification
        #self._pr_cond_1 = None  # Condenser pressure ratio 1 for cycle specification
        #self._pr_cond_2 = None  # Condenser pressure ratio 2 for cycle specification
        
        # Flow and Pressure Attributes
        self.evaporator_flow_rate = None  # m³/h
        self.condenser_flow_rate = None  # m³/h
        self.evaporator_pressure_drop = None  # kPa
        self.condenser_pressure_drop = None  # kPa
        self.max_evaporator_pressure = None  # bar
        self.max_condenser_pressure = None  # bar
        self.min_flow_rate = None  # m³/h
        self.max_flow_rate = None  # m³/h
        
        # Electrical Attributes
        self.nominal_power_input = None  # kW
        self.voltage = None  # V
        self.frequency = None  # Hz
        self.phases = None  # Number of phases
        self.full_load_current = None  # A
        self.max_current = None  # A
        self.standby_power = None  # W
        self.part_load_power_25 = None  # kW at 25% load
        self.part_load_power_50 = None  # kW at 50% load
        self.part_load_power_75 = None  # kW at 75% load
        
        # Refrigerant Attributes
        self.refrigerant_type = None  # e.g., 'R-134a', 'R-1233zd', 'R-410A', 'R-454B', 'R-32', 'R-1234ze(E)'
        self.refrigerant_charge = None  # kg
        self.gwp = None  # Global Warming Potential
        self.odp = None  # Ozone Depletion Potential
        
        # Physical Attributes
        self.length = None  # m
        self.width = None  # m
        self.height = None  # m
        self.operating_weight = None  # kg
        self.shipping_weight = None  # kg
        self.evaporator_water_volume = None  # L
        self.condenser_water_volume = None  # L
        self.footprint_area = None  # m²
        
        # Sound and Vibration
        self.sound_pressure_level = None  # dB(A)
        self.sound_power_level = None  # dB(A)
        
        # Certification and Standards
        self.certification_standard = None  # e.g., 'EN 14511', 'AHRI 550/590', 'Keymark'
        self.energy_label = None  # Energy efficiency class
        self.certified = False
        
        # Performance Map Data (from any certification source)
        self.performance_data = {}  # Dict to store performance at various conditions
        
        # Annual Performance Data
        self.annual_energy_consumption_heating = None  # kWh
        self.annual_energy_consumption_cooling = None  # kWh
        self.annual_heating_demand = None  # kWh
        self.annual_cooling_demand = None  # kWh
        
        # Control and Features
        self.control_type = None  # 'VFD', 'fixed speed', 'multi-stage'
        self.has_hot_gas_bypass = False
        self.has_economizer = False
        self.has_free_cooling = False
        self.min_unloading_capacity = None  # %
        
        # Maintenance and Service
        self.service_interval = None  # hours
        self.design_life = None  # years
        self.warranty_period = None  # years
        
        # Custom attributes storage for any additional data
        self.custom_attributes = {}
    
    def create_network(self):
        """Define the subsystem's connections."""
        # Initialize refrigerant type if not set
        if self.refrigerant_type is None:
            self.refrigerant_type = 'R134a'
        
        # Create the chiller cycle components
        cc = CycleCloser('cycle closer')  # cycle closer
        co = Condenser('condenser')  # heat sink
        ev = HeatExchanger('evaporator')  # heat source
        va = Valve('expansion valve')  # expansion valve
        cp = Compressor('compressor')  # compressor

        # Store direct references so offdesign/time-snapshot updates do not
        # need to depend on TESPy's internal subsystem component registry.
        self._evaporator = ev
        self._condenser = co
        self._compressor = cp
        self._expansion_valve = va

        # Connections of chiller refrigerant cycle
        c0 = Connection(va, 'out1', cc, 'in1', label='0')
        c1 = Connection(cc, 'out1', ev, 'in2', label='1')
        c2 = Connection(ev, 'out2', cp, 'in1', label='2')
        c3 = Connection(cp, 'out1', co, 'in1', label='3')
        c4 = Connection(co, 'out1', va, 'in1', label='4')
        
        
        self.add_conns(c0, c1, c2, c3, c4)

        # Connections to network and cooling tower
        # self.inlet has outlets: out1 (chilled water), out2 (condenser water)
        # self.outlet has inlets: in1 (chilled water), in2 (condenser water)
        c5 = Connection(self.inlet, 'out1', ev, 'in1', label='5')  # chilled water inlet to evaporator
        c6 = Connection(ev, 'out1', self.outlet, 'in1', label='6')  # chilled water outlet from evaporator
        c7 = Connection(self.inlet, 'out2', co, 'in2', label='7')  # condenser water inlet
        c8 = Connection(co, 'out2', self.outlet, 'in2', label='8')  # condenser water outlet
        
        self.add_conns(c5, c6, c7, c8)
        self._internal_connections = {
            "refrigerant_after_valve": c1,
            "refrigerant_after_evaporator": c2,
            "refrigerant_after_condenser": c4,
            "chw_in": c5,
            "chw_out": c6,
            "cw_in": c7,
            "cw_out": c8,
        }
        
        # ===================================================================
        # SET ALL CHILLER CYCLE PARAMETERS INSIDE THE SUBSYSTEM
        # ===================================================================
        
        # Refrigerant specification and evaporator outlet state
        # Point 2: saturated vapor at evaporating temperature
        c1.set_attr(T=self._T_evap)
        c2.set_attr(
            x=1.0,
            fluid={self._refrigerant: 1.0}
        )
        
        # Point 4: condensing temperature (Condenser enforces x=0 at out1)
        c4.set_attr(T=self._T_cond, x=0.0)
        # Replace with to avoid subcooling issues in TESPy:
        # co.set_attr(ttd_u=5)  # upper terminal temperature difference in K
        co.set_attr(subcooling=True)
        
        # Compressor isentropic efficiency
        cp.set_attr(eta_s=self._eta_s)
        
        # Evaporator cooling capacity (negative per TESPy sign convention:
        # heat leaves the hot side, i.e. chilled water)
        ev.set_attr(Q=-self._Q_evap)
        
        # Pressure ratios for both heat exchangers
        ev.set_attr(pr1=self._pr_evap_1, pr2=self._pr_evap_2)
        co.set_attr(pr1=self._pr_cond_1, pr2=self._pr_cond_2)

    @property
    def internal_connections(self):
        """Return named internal TESPy connections for advanced/offdesign setup."""
        if not self._internal_connections:
            raise RuntimeError("The chiller network connections have not been created yet.")
        return self._internal_connections

    @property
    def evaporator(self):
        """Return the internal evaporator heat exchanger component."""
        if self._evaporator is None:
            raise RuntimeError("The chiller evaporator has not been created yet.")
        return self._evaporator

    @property
    def condenser(self):
        """Return the internal condenser component."""
        if self._condenser is None:
            raise RuntimeError("The chiller condenser has not been created yet.")
        return self._condenser

    @property
    def compressor(self):
        """Return the internal compressor component."""
        if self._compressor is None:
            raise RuntimeError("The chiller compressor has not been created yet.")
        return self._compressor

    def configure_native_offdesign(
        self,
        evaporator_ttd_l: float = 5.0,
        condenser_ttd_u: float = 5.0,
        use_pressure_loss_characteristics: bool = True,
    ) -> None:
        """Configure TESPy-native design/offdesign switching for the chiller.

        The design solve fixes the nominal evaporating and condensing saturation
        temperatures to size the heat exchangers. Offdesign solves then release
        those saturation temperatures and use native heat-exchanger ``kA_char``
        and compressor ``eta_s_char`` attributes, so refrigerant temperatures,
        compressor power, condenser heat rejection, and COP respond to changing
        load and condenser-water conditions. The optional approach arguments are
        retained for API documentation and external checks; the actual design
        approach is implied by the fixed design temperatures.
        """
        conns = self.internal_connections
        conns["refrigerant_after_valve"].set_attr(T=self._T_evap, design=["T"])
        conns["refrigerant_after_condenser"].set_attr(T=self._T_cond, design=["T"])

        if use_pressure_loss_characteristics:
            self.evaporator.set_attr(
                ttd_l=evaporator_ttd_l,
                design=["pr1", "pr2", "ttd_l"],
                offdesign=["zeta1", "zeta2", "kA_char"],
            )
            self.condenser.set_attr(
                design=["pr1", "pr2"],
                offdesign=["zeta1", "zeta2", "kA_char"],
            )
        else:
            self.evaporator.set_attr(ttd_l=evaporator_ttd_l, design=["ttd_l"], offdesign=["kA_char"])
            self.condenser.set_attr(offdesign=["kA_char"])

        self.compressor.set_attr(eta_s=self._eta_s, design=["eta_s"], offdesign=["eta_s_char"])
        self.enable_compressor_characteristic_extrapolation()

    def enable_compressor_characteristic_extrapolation(self) -> None:
        """Allow compressor efficiency characteristic use outside default bounds.

        TESPy's default compressor ``eta_s_char`` line may warn when offdesign
        operation falls slightly outside its tabulated range. District-cooling
        time-series examples often include low-load snapshots, so this helper
        enables controlled extrapolation on the characteristic line used by
        ``eta_s_char``. If no line exists yet, a simple relative line around the
        design point is created and marked as extrapolatable.
        """
        eta_char = getattr(self.compressor, "eta_s_char", None)
        if eta_char is None:
            return
        char_func = getattr(eta_char, "char_func", None)
        if hasattr(char_func, "extrapolate"):
            char_func.extrapolate = True

    def update_Q_evap(self, new_Q_W: float) -> float:
        """Update evaporator cooling duty for a new operating snapshot.

        Parameters
        ----------
        new_Q_W : float
            Positive cooling load in W. The method applies the TESPy heat
            exchanger sign convention internally by setting the evaporator duty
            to ``-new_Q_W``.

        Returns
        -------
        float
            The positive cooling load stored on the chiller in W.
        """
        q_positive = float(new_Q_W)
        if q_positive < 0:
            raise ValueError("Chiller evaporator load must be provided as a positive value in W.")
        self._Q_evap = q_positive
        self.evaporator.set_attr(Q=-q_positive)
        return q_positive
    
    def import_from_csv(self, csv_file_path: str, 
                       delimiter: str = ',',
                       encoding: str = 'utf-8',
                       header_row: int = 0,
                       attribute_column: str = None,
                       value_column: str = None,
                       attribute_mapping: Dict[str, str] = None,
                       skip_unknown: bool = True) -> bool:
        """
        Import chiller data from a generic CSV file.
        
        This method supports various CSV formats from different certification standards.
        It can handle simple two-column format (attribute, value) or more complex
        formats with custom column mappings.
        
        Parameters
        ----------
        csv_file_path : str
            Path to the CSV file
        delimiter : str, optional
            CSV delimiter character (default: ',')
        encoding : str, optional
            File encoding (default: 'utf-8')
        header_row : int, optional
            Row index of the header (0-indexed). Use None if no header. (default: 0)
        attribute_column : str or int, optional
            Name or index of the column containing attribute names.
            If None, assumes first column (default: None)
        value_column : str or int, optional
            Name or index of the column containing values.
            If None, assumes second column (default: None)
        attribute_mapping : dict, optional
            Dictionary mapping CSV attribute names to chiller attribute names.
            Example: {'Model Number': 'model_name', 'COP': 'nominal_cop'}
            If None, attempts direct mapping (default: None)
        skip_unknown : bool, optional
            If True, skip unknown attributes. If False, store in custom_attributes
            (default: True)
        
        Returns
        -------
        bool
            True if import successful, False otherwise
        
        Examples
        --------
        Simple two-column CSV:
        >>> chiller.import_from_csv('data.csv')
        
        CSV with custom delimiter:
        >>> chiller.import_from_csv('data.csv', delimiter=';')
        
        CSV with column names:
        >>> chiller.import_from_csv('data.csv', 
        ...                         attribute_column='Parameter',
        ...                         value_column='Value')
        
        CSV with attribute mapping:
        >>> mapping = {'Model Number': 'model_name', 'COP': 'nominal_cop'}
        >>> chiller.import_from_csv('data.csv', attribute_mapping=mapping)
        """
        try:
            with open(csv_file_path, 'r', encoding=encoding) as csvfile:
                reader = csv.reader(csvfile, delimiter=delimiter)
                
                # Read header if specified
                header = None
                if header_row is not None:
                    for _ in range(header_row + 1):
                        header = next(reader, None)
                
                # Determine column indices
                if header and attribute_column and isinstance(attribute_column, str):
                    attr_col_idx = header.index(attribute_column)
                elif isinstance(attribute_column, int):
                    attr_col_idx = attribute_column
                else:
                    attr_col_idx = 0
                
                if header and value_column and isinstance(value_column, str):
                    val_col_idx = header.index(value_column)
                elif isinstance(value_column, int):
                    val_col_idx = value_column
                else:
                    val_col_idx = 1
                
                imported_count = 0
                skipped_count = 0
                
                for row in reader:
                    if len(row) <= max(attr_col_idx, val_col_idx):
                        continue
                    
                    csv_attr_name = row[attr_col_idx].strip()
                    csv_value = row[val_col_idx].strip()
                    
                    if not csv_attr_name or not csv_value:
                        continue
                    
                    # Apply attribute mapping if provided
                    if attribute_mapping and csv_attr_name in attribute_mapping:
                        chiller_attr_name = attribute_mapping[csv_attr_name]
                    else:
                        # Try to normalize the attribute name
                        chiller_attr_name = self._normalize_attribute_name(csv_attr_name)
                    
                    # Parse and set the value
                    parsed_value = self._parse_value(csv_value)
                    
                    if hasattr(self, chiller_attr_name):
                        setattr(self, chiller_attr_name, parsed_value)
                        imported_count += 1
                    elif not skip_unknown:
                        # Store in custom_attributes if not skipping unknown
                        self.custom_attributes[csv_attr_name] = parsed_value
                        imported_count += 1
                    else:
                        skipped_count += 1
                
                self.certified = True
                print(f"Successfully imported {imported_count} attributes")
                if skipped_count > 0:
                    print(f"Skipped {skipped_count} unknown attributes")
                return True
                
        except FileNotFoundError:
            print(f"Error: File '{csv_file_path}' not found")
            return False
        except Exception as e:
            print(f"Error importing CSV data: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def _normalize_attribute_name(self, csv_name: str) -> str:
        """
        Normalize CSV attribute name to match chiller attribute names.
        
        Converts various naming conventions to snake_case and handles
        common variations.
        
        Parameters
        ----------
        csv_name : str
            Attribute name from CSV
        
        Returns
        -------
        str
            Normalized attribute name
        """
        # Convert to lowercase
        normalized = csv_name.lower()
        
        # Replace spaces, hyphens, and other separators with underscores
        normalized = normalized.replace(' ', '_')
        normalized = normalized.replace('-', '_')
        normalized = normalized.replace('/', '_')
        normalized = normalized.replace('(', '')
        normalized = normalized.replace(')', '')
        
        # Remove multiple consecutive underscores
        while '__' in normalized:
            normalized = normalized.replace('__', '_')
        
        # Remove leading/trailing underscores
        normalized = normalized.strip('_')
        
        # Handle common variations
        attribute_aliases = {
            'model': 'model_name',
            'model_number': 'model_name',
            'model_no': 'model_name',
            'make': 'manufacturer',
            'brand': 'manufacturer',
            'type': 'chiller_type',
            'chiller_model': 'model_name',
            'capacity': 'nominal_cooling_capacity',
            'cooling_capacity': 'nominal_cooling_capacity',
            'rated_capacity': 'nominal_cooling_capacity',
            'heating_capacity': 'nominal_heating_capacity',
            'cop': 'nominal_cop',
            'eer': 'nominal_eer',
            'power': 'nominal_power_input',
            'power_input': 'nominal_power_input',
            'power_consumption': 'nominal_power_input',
            'refrigerant': 'refrigerant_type',
            'charge': 'refrigerant_charge',
            'weight': 'operating_weight',
            'compressor': 'compressor_type',
        }
        
        if normalized in attribute_aliases:
            return attribute_aliases[normalized]
        
        return normalized
    
    def _parse_value(self, value: str) -> Any:
        """
        Parse string value to appropriate Python type.
        
        Parameters
        ----------
        value : str
            String value to parse
        
        Returns
        -------
        Parsed value (int, float, bool, or str)
        """
        value = value.strip()
        
        if not value:
            return None
        
        # Try boolean
        if value.lower() in ('true', 'yes', 'y', '1'):
            return True
        if value.lower() in ('false', 'no', 'n', '0'):
            return False
        
        # Try integer
        try:
            return int(value)
        except ValueError:
            pass
        
        # Try float
        try:
            return float(value)
        except ValueError:
            pass
        
        # Return as string
        return value
    
    def set_performance_data(self, condition: str, value: Any):
        """
        Set performance data for a specific operating condition.
        
        Parameters
        ----------
        condition : str
            Description of the operating condition (e.g., 'COP_at_A7W35', 'capacity_50%_load')
        value : Any
            Performance value
        
        Examples
        --------
        >>> chiller.set_performance_data('COP_at_A7W35', 5.8)
        >>> chiller.set_performance_data('capacity_at_25%_load', 125)
        """
        self.performance_data[condition] = value
    
    def get_performance_data(self, condition: str) -> Optional[Any]:
        """
        Retrieve performance data for a specific operating condition.
        
        Parameters
        ----------
        condition : str
            Description of the operating condition
        
        Returns
        -------
        Performance value or None if not found
        
        Examples
        --------
        >>> cop = chiller.get_performance_data('COP_at_A7W35')
        >>> capacity = chiller.get_performance_data('capacity_at_25%_load')
        """
        return self.performance_data.get(condition)
    
    def list_performance_data(self) -> Dict[str, Any]:
        """
        Get all available performance data.
        
        Returns
        -------
        dict
            Dictionary of all performance data points
        """
        return self.performance_data.copy()
    
    def export_to_csv(self, file_path: str, 
                     include_none: bool = False,
                     include_performance_data: bool = True,
                     include_custom_attributes: bool = True):
        """
        Export chiller attributes to CSV file.
        
        Parameters
        ----------
        file_path : str
            Output CSV file path
        include_none : bool, optional
            Include attributes with None values (default: False)
        include_performance_data : bool, optional
            Include performance data dictionary (default: True)
        include_custom_attributes : bool, optional
            Include custom attributes (default: True)
        
        Examples
        --------
        >>> chiller.export_to_csv('chiller_data.csv')
        """
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['Attribute', 'Value'])
            
            # Export standard attributes
            for attr_name in dir(self):
                if (not attr_name.startswith('_') and 
                    not callable(getattr(self, attr_name)) and
                    attr_name not in ['comps', 'conns', 'inlet', 'outlet', 
                                     'performance_data', 'custom_attributes']):
                    attr_value = getattr(self, attr_name)
                    if attr_value is not None or include_none:
                        if not isinstance(attr_value, (Subsystem, Connection)):
                            writer.writerow([attr_name, attr_value])
            
            # Export performance data
            if include_performance_data:
                for condition, value in self.performance_data.items():
                    writer.writerow([f'performance_data.{condition}', value])
            
            # Export custom attributes
            if include_custom_attributes:
                for attr_name, value in self.custom_attributes.items():
                    writer.writerow([f'custom.{attr_name}', value])
        
        print(f"Attributes exported to {file_path}")
    
    def export_to_json(self, file_path: str,
                      include_none: bool = False,
                      include_performance_data: bool = True,
                      include_custom_attributes: bool = True):
        """
        Export chiller attributes to JSON file.
        
        Parameters
        ----------
        file_path : str
            Output JSON file path
        include_none : bool, optional
            Include attributes with None values (default: False)
        include_performance_data : bool, optional
            Include performance data dictionary (default: True)
        include_custom_attributes : bool, optional
            Include custom attributes (default: True)
        
        Examples
        --------
        >>> chiller.export_to_json('chiller_data.json')
        """
        data = {}
        
        # Export standard attributes
        for attr_name in dir(self):
            if (not attr_name.startswith('_') and 
                not callable(getattr(self, attr_name)) and
                attr_name not in ['comps', 'conns', 'inlet', 'outlet',
                                 'performance_data', 'custom_attributes']):
                attr_value = getattr(self, attr_name)
                if attr_value is not None or include_none:
                    if not isinstance(attr_value, (Subsystem, Connection)):
                        data[attr_name] = attr_value
        
        # Export performance data
        if include_performance_data and self.performance_data:
            data['performance_data'] = self.performance_data
        
        # Export custom attributes
        if include_custom_attributes and self.custom_attributes:
            data['custom_attributes'] = self.custom_attributes
        
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        
        print(f"Attributes exported to {file_path}")
    
    def import_from_json(self, file_path: str):
        """
        Import chiller attributes from JSON file.
        
        Parameters
        ----------
        file_path : str
            Path to JSON file with chiller attributes
        
        Examples
        --------
        >>> chiller.import_from_json('chiller_data.json')
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            imported_count = 0
            
            for key, value in data.items():
                if key == 'performance_data':
                    self.performance_data = value
                    imported_count += len(value)
                elif key == 'custom_attributes':
                    self.custom_attributes = value
                    imported_count += len(value)
                elif hasattr(self, key):
                    setattr(self, key, value)
                    imported_count += 1
            
            print(f"Successfully imported {imported_count} attributes from {file_path}")
            return True
        
        except FileNotFoundError:
            print(f"Error: File '{file_path}' not found")
            return False
        except Exception as e:
            print(f"Error importing JSON: {str(e)}")
            return False
    
    def get_summary(self) -> str:
        """
        Get a formatted summary of key chiller attributes.
        
        Returns
        -------
        str
            Formatted summary string
        """
        summary = []
        summary.append(f"Chiller: {self.label}")
        summary.append("=" * 50)
        
        if self.model_id:
            summary.append(f"Model ID: {self.model_id}")
        if self.manufacturer:
            summary.append(f"Manufacturer: {self.manufacturer}")
        if self.model_name:
            summary.append(f"Model Name: {self.model_name}")
        if self.chiller_type:
            summary.append(f"Type: {self.chiller_type}")
        if self.compressor_type:
            summary.append(f"Compressor: {self.compressor_type}")
        
        summary.append("")
        summary.append("Performance:")
        if self.nominal_cooling_capacity:
            summary.append(f"  Cooling Capacity: {self.nominal_cooling_capacity} kW")
        if self.nominal_cop:
            summary.append(f"  COP: {self.nominal_cop}")
        if self.nominal_eer:
            summary.append(f"  EER: {self.nominal_eer}")
        if self.iplv:
            summary.append(f"  IPLV: {self.iplv}")
        if self.scop:
            summary.append(f"  SCOP: {self.scop}")
        
        if self.nominal_power_input:
            summary.append(f"  Power Input: {self.nominal_power_input} kW")
        
        summary.append("")
        summary.append("Refrigerant:")
        if self.refrigerant_type:
            summary.append(f"  Type: {self.refrigerant_type}")
        if self.refrigerant_charge:
            summary.append(f"  Charge: {self.refrigerant_charge} kg")
        if self.gwp:
            summary.append(f"  GWP: {self.gwp}")
        
        if self.certification_standard:
            summary.append("")
            summary.append(f"Certification: {self.certification_standard}")
        
        if self.certified:
            summary.append("Certified: Yes")
        
        if self.performance_data:
            summary.append(f"Performance Data Points: {len(self.performance_data)}")
        
        if self.custom_attributes:
            summary.append(f"Custom Attributes: {len(self.custom_attributes)}")
        
        return "\n".join(summary)
    
    def __str__(self):
        """String representation of the chiller."""
        return self.get_summary()
    
    def __repr__(self):
        """Detailed representation of the chiller."""
        return f"Chiller(label='{self.label}', model_id='{self.model_id}')"


# Example usage and testing
if __name__ == "__main__":
    print("Enhanced TESPy Chiller Class - Generic CSV Import")
    print("=" * 60)
    print()
    
    # Example 1: Basic usage
    print("Example 1: Basic chiller")
    chiller1 = Chiller('basic_chiller')
    print(chiller1)
    print()
    
    # Example 2: Chiller with attributes
    print("Example 2: Chiller with specified attributes")
    chiller2 = Chiller('my_chiller',
                       model_name='ABC-500',
                       manufacturer='ChillerCorp',
                       nominal_cooling_capacity=500,
                       nominal_cop=5.5,
                       refrigerant_type='R-1233zd',
                       compressor_type='centrifugal',
                       chiller_type='water/water')
    print(chiller2)
    print()
    
    # Example 3: Export attributes
    print("Example 3: Export attributes")
    chiller2.export_to_json('chiller_example.json')
    chiller2.export_to_csv('chiller_example.csv')
    print()
    
    # Example 4: Generic CSV import
    print("Example 4: Generic CSV import capability")
    print("To import from any CSV:")
    chiller1.import_from_csv('my_model/components/example_ahri_certification.csv')
    print(chiller1)
    print("  chiller.import_from_csv('data.csv', delimiter=';')")
    print("  chiller.import_from_csv('data.csv', attribute_column='Parameter', value_column='Value')")