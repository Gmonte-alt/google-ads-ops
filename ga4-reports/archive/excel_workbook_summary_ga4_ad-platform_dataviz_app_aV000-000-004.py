# file name: ga4-reports/excel_workbook_summary_ga4_ad-platform_dataviz.py
# version: V000-000-004
# Notes: Creates Chart visualizations on the pivot_sheet - builds the full list of charts on the first column

### Enhanced Python Script

## Now, I’ll provide the complete Python script that integrates with the updated VBA code and dynamically creates metric groupings on all four charts.

import win32com.client as win32
import os

# Paths to the workbooks
final_report_path = r'C:\Users\GilbertMontemayor\workspace\google-ads-ops\ga4-reports\output\ga4_summary_metrics_formatted.xlsx'

# Check if the final report file exists
if not os.path.exists(final_report_path):
    print(f"Error: Final report workbook not found at {final_report_path}. Please check the path.")
    exit(1)

vba_code_pivot_table = """
Sub CreatePivotTableWithSharedCache(metricName As String, pivotTableName As String, topPosition As Double, leftPosition As Double)
    Dim wsData As Worksheet
    Dim wsPivot As Worksheet
    Dim pivotCache As PivotCache
    Dim pivotTable As PivotTable
    Dim lastRow As Long
    Dim lastCol As Long
    Dim dataRange As Range

    ' Set your data worksheet
    Set wsData = ThisWorkbook.Sheets("--DATA--EXP--")

    ' Find the last row and column of the data
    lastRow = wsData.Cells(wsData.Rows.Count, 1).End(xlUp).Row
    lastCol = wsData.Cells(1, wsData.Columns.Count).End(xlToLeft).Column

    ' Define the data range
    Set dataRange = wsData.Range(wsData.Cells(1, 1), wsData.Cells(lastRow, lastCol))
    
    ' Check if the pivot cache already exists (reuse it if it does)
    If Not pivotCache Is Nothing Then
        ' Do nothing, we will reuse the existing pivot cache
    Else
        ' Create a new Pivot Cache (this will only happen once)
        Set pivotCache = ThisWorkbook.PivotCaches.Create(SourceType:=xlDatabase, SourceData:=dataRange)
    End If

    ' Check if the sheet "Pivot_Sheet" exists, and create it if it doesn't
    On Error Resume Next
    Set wsPivot = ThisWorkbook.Sheets("Pivot_Sheet")
    On Error GoTo 0
    If wsPivot Is Nothing Then
        Set wsPivot = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        wsPivot.Name = "Pivot_Sheet"
    End If
    
    ' Check if the pivot table already exists
    ptExists = False
    On Error Resume Next
    Set pivotTable = wsPivot.PivotTables(pivotTableName)
    If Not pivotTable Is Nothing Then ptExists = True
    On Error GoTo 0
    
    ' Delete existing PivotTable if it exists
    If ptExists Then
        wsPivot.PivotTables(pivotTableName).TableRange2.Clear
    End If

    ' Create the Pivot Table using the shared pivot cache
    Set pivotTable = wsPivot.PivotTables.Add(PivotCache:=pivotCache, _
                    TableDestination:=wsPivot.Cells(topPosition, leftPosition), _
                    TableName:=pivotTableName)

    ' Add fields to the Pivot Table (replace these fields with your relevant fields)
    pivotTable.PivotFields("ISO_Week").Orientation = xlRowField  ' Row Field
    pivotTable.PivotFields("ISO_Year").Orientation = xlColumnField  ' Column Field
    pivotTable.PivotFields("Ad Platform").Orientation = xlPageField  ' Ad Platform filter Field
    pivotTable.PivotFields("Period").Orientation = xlPageField  ' Period filter Field
    pivotTable.PivotFields("Paid or Non-Paid").Orientation = xlPageField  ' Paid or Non-Paid filter Field
    pivotTable.PivotFields("Brand or Non-Brand").Orientation = xlPageField  ' Brand or Non-Brand filter Field
    pivotTable.PivotFields("Paid Media Type").Orientation = xlPageField  ' Paid Media Type filter Field
    pivotTable.PivotFields("Campaign_Group").Orientation = xlPageField  ' Campaign_Group filter Field
    pivotTable.PivotFields("Total").Orientation = xlPageField  ' Total filter Field
    pivotTable.PivotFields("Tenant or Landlord").Orientation = xlPageField  ' Tenant or Landlord filter Field
    pivotTable.PivotFields("Channel_Group").Orientation = xlPageField  ' Channel_Group filter Field

    ' Data Field (dynamically using the metricName passed from the Python script)
    With pivotTable.PivotFields(metricName)
        .Orientation = xlDataField
        .Function = xlSum  ' Set to Sum
        .NumberFormat = "0.00"  ' Set number format (2 decimal places)
    End With
    
    ' Remove Grand Totals for both rows and columns
    pivotTable.RowGrand = False  ' Disable Grand Total for rows
    pivotTable.ColumnGrand = False  ' Disable Grand Total for columns

    MsgBox "Pivot Table created successfully for " & metricName
End Sub
"""

vba_code_yoy = """
Sub CreateReferenceRangeAndYoY(pivotTableName As String, metricName As String, rangeStartRow As Integer, rangeStartColumn As Integer)
    Dim wsPivot As Worksheet
    Dim wsData As Worksheet
    Dim lastRow As Long
    Dim lastCol As Long
    Dim pivotTable As PivotTable
    Dim rangeHeaderRow As Range
    Dim currentRow As Integer
    Dim pivotRange As Range
    Dim i As Integer

    ' Retrieve and dynamically assign the top two years from the Pivot Table for ISO_Year field
    Dim pivot_iso_year_field As PivotField
    Dim pivot_years() As Integer
    Dim highest_year As Integer
    Dim second_highest_year As Integer

    ' Set the pivot table worksheet
    Set wsPivot = ThisWorkbook.Sheets("Pivot_Sheet")

    ' Find the pivot table by its name
    Set pivotTable = wsPivot.PivotTables(pivotTableName)

    ' Get the last row and column of the pivot table data
    lastRow = pivotTable.TableRange2.Rows.Count
    lastCol = pivotTable.TableRange2.Columns.Count

    ' Reference where the data range will begin
    currentRow = rangeStartRow
    Set rangeHeaderRow = wsPivot.Cells(currentRow, rangeStartColumn)
    
    ' Get the PivotField for ISO_Year
    Set pivot_iso_year_field = pivotTable.PivotFields("ISO_Year")

    ' ReDim pivot_years array to hold the number of years in the PivotField
    ReDim pivot_years(1 To pivot_iso_year_field.PivotItems.Count)

    ' Populate the pivot_years array with the year values from the PivotItems
    For i = 1 To pivot_iso_year_field.PivotItems.Count
        pivot_years(i) = CInt(pivot_iso_year_field.PivotItems(i).Name)
    Next i

    ' Sort the years in descending order
    Call BubbleSortDescending(pivot_years) ' We will implement this helper function next

    ' Assign the highest and second highest years
    highest_year = pivot_years(1)
    second_highest_year = pivot_years(2)

    ' Create headers for the range
    rangeHeaderRow.Value = "Row Labels"
    wsPivot.Cells(currentRow, rangeStartColumn + 1).Value = highest_year
    wsPivot.Cells(currentRow, rangeStartColumn + 2).Value = second_highest_year
    wsPivot.Cells(currentRow, rangeStartColumn + 3).Value = "YoY"

    ' Loop through to populate 52 rows for ISO_Week and create formulas for the other columns
    For i = 1 To 52
        currentRow = currentRow + 1

        ' ISO_Week: Increment values from 1 to 52
        If i = 1 Then
            wsPivot.Cells(currentRow, rangeStartColumn).Value = 1
        Else
            wsPivot.Cells(currentRow, rangeStartColumn).Formula = "=" & wsPivot.Cells(currentRow - 1, rangeStartColumn).Address & "+1"
        End If
        
        ' 2024 Data: Reference the metric for 2024 from the Pivot Table using the metricName variable
        wsPivot.Cells(currentRow, rangeStartColumn + 1).Formula = "=IFERROR(GETPIVOTDATA(" & Chr(34) & metricName & Chr(34) & "," & pivotTable.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 1).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & "),0)"

        ' 2023 Data: Reference the metric for 2023 from the Pivot Table
        wsPivot.Cells(currentRow, rangeStartColumn + 2).Formula = "=IFERROR(GETPIVOTDATA(" & Chr(34) & metricName & Chr(34) & "," & pivotTable.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 2).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & "),0)"


        ' YoY Calculation: (2024 value / 2023 value) - 1
        wsPivot.Cells(currentRow, rangeStartColumn + 3).Formula = "=IFERROR((" & wsPivot.Cells(currentRow, rangeStartColumn + 1).Address & "/" & wsPivot.Cells(currentRow, rangeStartColumn + 2).Address & ")-1,0)"
        wsPivot.Cells(currentRow, rangeStartColumn + 3).NumberFormat = "0.00%"  ' Format as percentage
    Next i

    MsgBox "Reference range and YoY calculation created successfully for " & metricName
End Sub

"""

vba_bubble_sort_desc = """
Sub BubbleSortDescending(arr() As Integer)
    Dim i As Integer, j As Integer
    Dim temp As Integer
    For i = LBound(arr) To UBound(arr) - 1
        For j = i + 1 To UBound(arr)
            If arr(i) < arr(j) Then
                ' Swap the elements
                temp = arr(i)
                arr(i) = arr(j)
                arr(j) = temp
            End If
        Next j
    Next i
End Sub

"""

vba_code_derived_metric_range = """
Sub CreateDerivedRange(pivotTableName_1 As String, pivotTableName_2 As String, metric_1 As String, metric_2 As String, derived_range_name As String, rangeStartRow As Integer, rangeStartColumn As Integer)
    Dim wsPivot As Worksheet
    Dim currentRow As Integer
    Dim i As Integer
    Dim pivotTable_1 As PivotTable
    Dim pivotTable_2 As PivotTable
    
    ' Set the pivot table worksheet
    Set wsPivot = ThisWorkbook.Sheets("Pivot_Sheet")

    ' Find the pivot tables by their names
    Set pivotTable_1 = wsPivot.PivotTables(pivotTableName_1)
    Set pivotTable_2 = wsPivot.PivotTables(pivotTableName_2)
    
    ' Reference where the data range will begin
    currentRow = rangeStartRow
    Set rangeHeaderRow = wsPivot.Cells(currentRow, rangeStartColumn)
    
    ' Retrieve and dynamically assign the top two years from the Pivot Table for ISO_Year field
    Dim pivot_iso_year_field As PivotField
    Dim pivot_years() As Integer
    Dim highest_year As Integer
    Dim second_highest_year As Integer

     ' Get the last row and column of the pivot table data
    lastRow = pivotTable_1.TableRange2.Rows.Count
    lastCol = pivotTable_1.TableRange2.Columns.Count
    
    ' Get the PivotField for ISO_Year
    Set pivot_iso_year_field = pivotTable_1.PivotFields("ISO_Year")

    ' ReDim pivot_years array to hold the number of years in the PivotField
    ReDim pivot_years(1 To pivot_iso_year_field.PivotItems.Count)

    ' Populate the pivot_years array with the year values from the PivotItems
    ReDim pivot_years(1 To pivot_iso_year_field.PivotItems.Count)
    For i = 1 To pivot_iso_year_field.PivotItems.Count
        pivot_years(i) = CInt(pivot_iso_year_field.PivotItems(i).Name)
    Next i

    ' Sort the years in descending order
    Call BubbleSortDescending(pivot_years)

    ' Assign the highest and second highest years
    highest_year = pivot_years(1)
    second_highest_year = pivot_years(2)

    ' Create headers for the range
    rangeHeaderRow.Value = "Row Labels"
    wsPivot.Cells(currentRow, rangeStartColumn + 1).Value = highest_year
    wsPivot.Cells(currentRow, rangeStartColumn + 2).Value = second_highest_year
    wsPivot.Cells(currentRow, rangeStartColumn + 3).Value = "YoY"
    
    ' Loop to populate the range for 52 weeks
    For i = 1 To 52
        currentRow = rangeStartRow + i
        
        ' ISO_Week: Increment from 1 to 52
        wsPivot.Cells(currentRow, rangeStartColumn).Value = i
        
        ' 2024: Reference the primary metric range (assumed from previous ranges)
        wsPivot.Cells(currentRow, rangeStartColumn + 1).Formula = "=IFERROR(GETPIVOTDATA(" & Chr(34) & metric_1 & Chr(34) & "," & pivotTable_1.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 1).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & ")/GETPIVOTDATA(" & Chr(34) & metric_2 & Chr(34) & "," & pivotTable_2.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 1).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & "),0)"
        
        ' 2023: Reference the primary metric range (assumed from previous ranges)
        wsPivot.Cells(currentRow, rangeStartColumn + 2).Formula = "=IFERROR(GETPIVOTDATA(" & Chr(34) & metric_1 & Chr(34) & "," & pivotTable_1.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 2).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & ")/GETPIVOTDATA(" & Chr(34) & metric_2 & Chr(34) & "," & pivotTable_2.TableRange2.Cells(1, 1).Address & ",""ISO_Year""," & wsPivot.Cells(rangeStartRow, rangeStartColumn + 2).Address & ",""ISO_Week""," & wsPivot.Cells(currentRow, rangeStartColumn).Address & "),0)"
        
        ' YoY Calculation: (2024 value / 2023 value) - 1
        wsPivot.Cells(currentRow, rangeStartColumn + 3).Formula = "=IFERROR((" & wsPivot.Cells(currentRow, rangeStartColumn + 1).Address & "/" & wsPivot.Cells(currentRow, rangeStartColumn + 2).Address & ")-1,0)"
        wsPivot.Cells(currentRow, rangeStartColumn + 3).NumberFormat = "0.00%"  ' Format as percentage
    Next i
    
    MsgBox "Derived range created successfully for " & derived_range_name
End Sub
"""

vba_code_shared_cache = """
Sub CreatePivotTablesWithSharedCache(metrics As Variant)
    Dim wsData As Worksheet
    Dim wsPivot As Worksheet
    Dim pivotCache As PivotCache
    Dim pivotTable As PivotTable
    Dim lastRow As Long
    Dim lastCol As Long
    Dim dataRange As Range
    Dim metricName As String
    Dim pivotTableName As String
    Dim topPosition As Double
    Dim leftPosition As Double
    Dim i As Integer

    ' Set your data worksheet
    Set wsData = ThisWorkbook.Sheets("--DATA--EXP--")

    ' Find the last row and column of the data
    lastRow = wsData.Cells(wsData.Rows.Count, 1).End(xlUp).Row
    lastCol = wsData.Cells(1, wsData.Columns.Count).End(xlToLeft).Column

    ' Define the data range
    Set dataRange = wsData.Range(wsData.Cells(1, 1), wsData.Cells(lastRow, lastCol))

    ' Create a single Pivot Cache to be shared across all pivot tables
    Set pivotCache = ThisWorkbook.PivotCaches.Create(SourceType:=xlDatabase, SourceData:=dataRange)

    ' Check if the sheet "Pivot_Sheet" exists, and create it if it doesn't
    On Error Resume Next
    Set wsPivot = ThisWorkbook.Sheets("Pivot_Sheet")
    On Error GoTo 0
    If wsPivot Is Nothing Then
        Set wsPivot = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        wsPivot.Name = "Pivot_Sheet"
    End If

    ' Set the initial positions for the first pivot table
    topPosition = 2
    leftPosition = 2

    ' Loop through the metrics to create pivot tables with the shared cache
    For i = LBound(metrics) To UBound(metrics)
        metricName = metrics(i)
        pivotTableName = "PivotTable_" & metricName

        ' Check if the pivot table already exists
        On Error Resume Next
        Set pivotTable = wsPivot.PivotTables(pivotTableName)
        If Not pivotTable Is Nothing Then
            wsPivot.PivotTables(pivotTableName).TableRange2.Clear
        End If
        On Error GoTo 0

        ' Create the Pivot Table using the shared pivot cache
        Set pivotTable = wsPivot.PivotTables.Add(PivotCache:=pivotCache, _
                        TableDestination:=wsPivot.Cells(topPosition, leftPosition), _
                        TableName:=pivotTableName)

        ' Add fields to the Pivot Table (adjust fields as necessary)
        pivotTable.PivotFields("ISO_Week").Orientation = xlRowField  ' Row Field
        pivotTable.PivotFields("ISO_Year").Orientation = xlColumnField  ' Column Field
        pivotTable.PivotFields("Ad Platform").Orientation = xlPageField  ' Ad Platform filter Field
        pivotTable.PivotFields("Period").Orientation = xlPageField  ' Period filter Field
        pivotTable.PivotFields("Paid or Non-Paid").Orientation = xlPageField  ' Paid or Non-Paid filter Field
        pivotTable.PivotFields("Brand or Non-Brand").Orientation = xlPageField  ' Brand or Non-Brand filter Field
        pivotTable.PivotFields("Paid Media Type").Orientation = xlPageField  ' Paid Media Type filter Field
        pivotTable.PivotFields("Campaign_Group").Orientation = xlPageField  ' Campaign_Group filter Field
        pivotTable.PivotFields("Total").Orientation = xlPageField  ' Total filter Field
        pivotTable.PivotFields("Tenant or Landlord").Orientation = xlPageField  ' Tenant or Landlord filter Field
        pivotTable.PivotFields("Channel_Group").Orientation = xlPageField  ' Channel_Group filter Field

        ' Data Field (using the metric name passed from the Python script)
        With pivotTable.PivotFields(metricName)
            .Orientation = xlDataField
            .Function = xlSum  ' Set to Sum
            .NumberFormat = "0.00"  ' Set number format (2 decimal places)
        End With

        ' Remove Grand Totals for both rows and columns
        pivotTable.RowGrand = False  ' Disable Grand Total for rows
        pivotTable.ColumnGrand = False  ' Disable Grand Total for columns

        ' Increment the position for the next pivot table
        leftPosition = leftPosition + 5  ' Adjust column position for the next pivot table
    Next i

    MsgBox "All Pivot Tables created successfully with shared cache."
End Sub
"""

vba_code_chart_prefix = """
Sub CreateFilterInformation()
    Dim ws As Worksheet
    Dim slicerNames As Variant
    Dim filterRowStart As Integer
    Dim filteredValuesRowStart As Integer
    Dim chartPrefixRowStart As Integer
    Dim i As Integer
    Dim lastCol As Integer
    Dim startColumn As Integer
    Dim lookupRange As String
    Dim numFilters As Integer
    Dim endColumn As Integer
    Dim endRow As Long

    ' Set the target worksheet
    Set ws = ThisWorkbook.Sheets("Pivot_Sheet")

    ' Define the starting rows and columns
    filterRowStart = 1  ' First row for filter names
    filteredValuesRowStart = 2  ' Second row for filtered values
    chartPrefixRowStart = 3  ' Third row for Chart Prefix
    firstCol = 17  ' Column Q is the 17th column
    slicerStartCol = 18  ' Column R is the 18th column

    ' Define slicers to reference
    slicerNames = Array("Paid or Non-Paid", "Tenant or Landlord", "Paid Media Type", "Brand or Non-Brand", "Ad Platform", "Channel_Group", "Campaign_Group", "Period")
    numFilters = UBound(slicerNames) - LBound(slicerNames) + 1  ' Total number of slicers (8 in this case)

    ' Find the starting column of the first Pivot Table
    startColumn = ws.PivotTables("PivotTable_FF_Purchase_Event_Count").TableRange2.Column
    endColumn = startColumn + 1  ' We are looking at the next column for values

    ' Dynamically find the last row based on the number of filters
    endRow = numFilters + 1

    ' Create the lookup range dynamically (R1C1 notation)
    lookupRange = "R1C" & startColumn & ":R" & endRow & "C" & endColumn

    ' --------------------- Row 1: Filter Names --------------------- '
    ' Add filter names in row 1 starting from column R
    For i = LBound(slicerNames) To UBound(slicerNames)
        ws.Cells(filterRowStart, slicerStartCol + i).Value = slicerNames(i)
    Next i

    ' ---------------- Row 2: Filtered Values ---------------- '
    ' Add "Filtered Values:" in column Q of row 2
    ws.Cells(filteredValuesRowStart, firstCol).Value = "Filtered Values:"

    ' Use VLOOKUP to reference the visible slicer values in the first pivot table dynamically and hide "(All)" values (R1C1 notation)
    For i = LBound(slicerNames) To UBound(slicerNames)
        ws.Cells(filteredValuesRowStart, slicerStartCol + i).FormulaR1C1 = _
        "=IF(VLOOKUP(R" & filterRowStart & "C" & slicerStartCol + i & "," & lookupRange & ",2,FALSE)=" & Chr(34) & "(All)" & Chr(34) & "," & Chr(34) & Chr(34) & ",VLOOKUP(R" & filterRowStart & "C" & slicerStartCol + i & "," & lookupRange & ",2,FALSE))"
    Next i


    ' ---------------- Row 3: Chart Prefix ---------------- '
    ' Add "Chart Prefix:" in column Q of row 3
    ws.Cells(chartPrefixRowStart, firstCol).Value = "Chart Prefix:"

    ' Concatenate the slicer filtered values in row 2 from columns R through Y with hyphen as separator
    lastCol = slicerStartCol + UBound(slicerNames) ' Determine the last column for the slicers
    ws.Cells(chartPrefixRowStart, slicerStartCol).Formula = _
        "=TEXTJOIN("" - "",TRUE,R" & filteredValuesRowStart & ":Y" & filteredValuesRowStart & ")"

    MsgBox "Filter information and Chart Prefix created successfully!"
End Sub
"""
vba_code_chart_creation = """
Sub CreateLandlordCharts()
    Dim ws As Worksheet
    Dim foundCell As Range
    Dim rangeStartCol As Long
    Dim metric As String
    Dim chartTopPosition As Double
    Dim chart As ChartObject
    Dim chartXValues As Range
    Dim chartYValuesPrimary As Range
    Dim chartYValuesSecondary As Range
    Dim startRow As Long
    Dim numRows As Long
    Dim seriesName1 As String, seriesName2 As String
    Dim chartPrefix As String
    Dim i As Integer
    Dim landlordMetrics As Variant
    Dim firstChart As Boolean
    Dim rowOffset As Integer
    Dim padding As Integer

    ' Set your worksheet
    Set ws = ThisWorkbook.Sheets("Pivot_Sheet")

    ' Define the metrics list (Landlord metrics)
    landlordMetrics = Array("FF_Purchase_Event_Count", "Cost / FF_Purchase_Event_Count", "FF_Lead_Event_Count", "FF_Purchase_Event_Count / FF_Lead_Event_Count")

    ' Define the number of rows (52 rows standard)
    numRows = 52

    ' Track whether the section name has been added
    firstChart = True

    ' Set padding between chart and prefix
    padding = 3  ' You can adjust this to add more or fewer rows of padding

    ' Loop through each metric in the landlordMetrics array
    For i = LBound(landlordMetrics) To UBound(landlordMetrics)
        metric = landlordMetrics(i)

        ' Look for the metric in row 7, scan column by column
        Set foundCell = ws.Rows(7).Find(What:=metric, LookIn:=xlValues, LookAt:=xlWhole)

        ' Check if the metric was found
        If foundCell Is Nothing Then
            MsgBox "Metric '" & metric & "' not found in row 7."
            Exit Sub
        Else
            rangeStartCol = foundCell.Column  ' Get the starting column of the range
        End If

        ' Define the start row (5 rows below where the metric name was found)
        startRow = foundCell.Row + 5

        ' Define the X-axis range (first column) and series ranges (second, third, and fourth columns)
        Set chartXValues = ws.Range(ws.Cells(startRow + 1, rangeStartCol), ws.Cells(startRow + 1 + numRows - 1, rangeStartCol))  ' X-axis range (first column)
        Set chartYValuesPrimary = ws.Range(ws.Cells(startRow + 1, rangeStartCol + 1), ws.Cells(startRow + 1 + numRows - 1, rangeStartCol + 2))  ' Primary Y-axis (second and third columns)
        Set chartYValuesSecondary = ws.Range(ws.Cells(startRow + 1, rangeStartCol + 3), ws.Cells(startRow + 1 + numRows - 1, rangeStartCol + 3))  ' Secondary Y-axis (fourth column - YoY)

        ' Insert section name "Landlord Performance" only for the first chart in row 10, column E
        If firstChart Then
            ws.Cells(10, 5).Value = "Landlord Performance"
            ws.Cells(10, 5).Font.Size = 20
            firstChart = False
        End If

        ' Adjust the chart position dynamically based on the iteration, adding padding between the charts
        rowOffset = i * (20 + padding)  ' Adjust to add padding
        chartTopPosition = ws.Cells(13 + rowOffset, 5).Top  ' Column E, move down for each chart iteration

        ' Create the chart
        Set chart = ws.ChartObjects.Add(Left:=ws.Cells(13 + rowOffset, 5).Left, Top:=chartTopPosition, Width:=600, Height:=300)  ' Adjust position and size
        chart.Chart.SetSourceData Source:=chartYValuesPrimary ' chartXValues Changing to the Y value and observe what happens

        ' Add series for the primary Y-axis (2 columns for series values) and set as line chart
        With chart.Chart.SeriesCollection(1)
            .XValues = chartXValues  ' Set X-axis labels
            .Values = chartYValuesPrimary.Columns(1)  ' Use second column for first series
            .Name = "=" & ws.Cells(startRow, rangeStartCol + 1).Address(External:=True) ' Series name is now dynamically linked to the cell
            .ChartType = xlLine
            .AxisGroup = xlPrimary
        End With

        With chart.Chart.SeriesCollection(2)
            .XValues = chartXValues  ' Set X-axis labels
            .Values = chartYValuesPrimary.Columns(2)  ' Use third column for second series
            .Name = "=" & ws.Cells(startRow, rangeStartCol + 1).Address(External:=True) ' Series name is now dynamically linked to the cell
            .ChartType = xlLine
            .AxisGroup = xlPrimary
        End With

        ' Add series for the secondary Y-axis (YoY) and set as clustered column
        With chart.Chart.SeriesCollection.NewSeries
            .XValues = chartXValues  ' Set X-axis labels
            .Values = chartYValuesSecondary  ' Use fourth column for YoY series
            .Name = ws.Cells(startRow, rangeStartCol + 3).Value  ' Use the name from the first row of the YoY range
            .ChartType = xlColumnClustered
            .AxisGroup = xlSecondary
        End With

        ' Remove the x-axis title
        chart.Chart.Axes(xlCategory).HasTitle = False

        ' Change YoY series to percentage format and remove the secondary Y-axis title
        With chart.Chart.Axes(xlValue, xlSecondary)
            .HasTitle = False
            .TickLabels.NumberFormat = "0%"  ' Format as percentage with zero decimal places
        End With

        ' Remove primary Y-axis title
        chart.Chart.Axes(xlValue, xlPrimary).HasTitle = False

        ' Insert chart prefix concatenation formula in the row directly above the chart
        chartPrefix = "=R3 & "" - "" & " & ws.Cells(7, rangeStartCol).Address(False, False)
        ws.Cells(12 + rowOffset, 5).Formula = chartPrefix

        ' Set the chart title to reference the concatenation result in row 12
        chart.Chart.HasTitle = True
        chart.Chart.ChartTitle.Formula = "=" & ws.Cells(12 + rowOffset, 5).Address(External:=True)

        ' Move the legend to the bottom of the chart
        chart.Chart.HasLegend = True
        chart.Chart.Legend.Position = xlLegendPositionBottom

        MsgBox "Chart for '" & metric & "' created successfully!"
    Next i

End Sub
"""

# Open Excel application
excel_app = win32.Dispatch("Excel.Application")

# Set Excel to be visible (optional)
excel_app.Visible = True

# Open the Final Report Workbook
final_workbook = excel_app.Workbooks.Open(final_report_path)

# ------------------------------ PIVOT CODE INSERTION ------------------------------ # 
# Insert the VBA code for pivot tables
try:
    new_module_pivot = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module_pivot.Name = "PivotTableModule"
    new_module_pivot.CodeModule.AddFromString(vba_code_pivot_table)
    print("VBA code for pivot tables successfully added to the final report.")
except Exception as e:
    print(f"Error while adding pivot table VBA code: {e}")

# Define the metrics and starting positions
metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "Total Traveler Actions", "FF_BRSubmit_Event_Count", "Cost", "Sessions", "Clicks", "Impressions", "Impression_population"]  # Update with your metrics
pivot_top_position = 2  # Starting row for the first Pivot Table
pivot_left_position = 2  # Starting column for the first Pivot Table
pivot_table_width = 4  # Number of columns occupied by each Pivot Table
spacing_between_tables = 2  # Space between each table in terms of columns

# Loop through metrics to create separate pivot tables without overlap in columns
try:
    for metric in metrics:
        macro_name = f"{final_workbook.Name}!PivotTableModule.CreatePivotTableWithSharedCache" # CreatePivotTable"
        # Use unique pivot table names to avoid conflicts
        pivot_table_name = f"PivotTable_{metric}"
        
        # Run the macro with updated column position for each metric
        excel_app.Application.Run(macro_name, metric, pivot_table_name, pivot_top_position, pivot_left_position)
        
        # Increment the left position for the next pivot table (move right by the width of a table + spacing)
        pivot_left_position += pivot_table_width + spacing_between_tables
        print(f"Pivot Table for {metric} created successfully at column {pivot_left_position}.")
except Exception as e:
    print(f"Error while creating pivot table: {e}")


# ------------------------------ INSERT VBA CODE FOR SHARED PIVOT CACHE ------------------------------ #
try:
    # Insert the VBA code for shared cache pivot tables
    new_module = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module.Name = "SharedPivotCacheModule"
    new_module.CodeModule.AddFromString(vba_code_shared_cache)
    print("VBA code for shared pivot cache successfully added to the final report.")
except Exception as e:
    print(f"Error while adding shared pivot cache VBA code: {e}")

# ------------------------------ EXECUTE VBA MACRO ------------------------------ #

# Define the metrics list
metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "Total Traveler Actions", "FF_BRSubmit_Event_Count", "Cost", "Sessions", "Clicks", "Impressions", "Impression_population"]

# Call the macro with the metrics as argument
try:
    excel_app.Application.Run(f"{final_workbook.Name}!SharedPivotCacheModule.CreatePivotTablesWithSharedCache", metrics)
    print("VBA macro executed successfully.")
except Exception as e:
    print(f"Error while executing VBA macro: {e}")



# ------------------------------ RANGE PRIMARY METRICS CODE INSERTION ------------------------------ # 
# Insert VBA code for range reference to pivots
try:
    new_module_yoy = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module_yoy.Name = "ReferenceRangeModule"
    new_module_yoy.CodeModule.AddFromString(vba_code_yoy)
    print("VBA code for reference ranges and YoY successfully added.")
except Exception as e:
    print(f"Error while adding reference range VBA code: {e}")

# Insert the bubble sort module
try:
    new_module_bubble_sort = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module_bubble_sort.Name = "BubbleSortModule"
    new_module_bubble_sort.CodeModule.AddFromString(vba_bubble_sort_desc)
    print("VBA code for Bubble Sort added.")
except Exception as e:
    print(f"Error while adding Bubble Sort VBA code: {e}")

# Define the metrics and corresponding pivot tables
metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "Total Traveler Actions", "FF_BRSubmit_Event_Count", "Cost", "Sessions", "Clicks", "Impressions", "Impression_population"]  # Update with your metrics FF_BRSubmit_Event_Count
range_start_row = 12  # Starting row for the reference range # Changed from default value = 30
range_start_column = 56  # Starting column for the reference range (adjust as needed)

# Loop through metrics to create ranges for each pivot table
try:
    for metric in metrics:
        macro_name = f"{final_workbook.Name}!ReferenceRangeModule.CreateReferenceRangeAndYoY"
        pivot_table_name = f"PivotTable_{metric}"
        
        # Run the macro with updated row/column position for each metric
        excel_app.Application.Run(macro_name, pivot_table_name, metric, range_start_row, range_start_column)
        
        # Insert metric name two rows above the range
        wsRange = final_workbook.Sheets("Pivot_Sheet")
        wsRange.Cells(range_start_row -5, range_start_column).Value = metric
        
        # Increment the starting column for the next range (move right by a few columns)
        range_start_column += 5  # Adjust as needed to avoid overlap
        print(f"Reference range and YoY for {metric} created successfully.")
except Exception as e:
    print(f"Error while creating reference range: {e}")


# ------------------------------ RANGE DERIVED METRICS CODE INSERTION ------------------------------ # 

# Insert VBA code for range reference to pivots
try:
    new_module_yoy = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module_yoy.Name = "DerivedRangeModule"
    new_module_yoy.CodeModule.AddFromString(vba_code_derived_metric_range)
    print("VBA code for reference ranges and YoY successfully added.")
except Exception as e:
    print(f"Error while adding reference range VBA code: {e}")

# Define the derived metrics and starting positions
derived_metrics = ["FF_Purchase_Event_Count / FF_Lead_Event_Count", "Cost / FF_Purchase_Event_Count", "Cost / Total Traveler Actions"] # CAC, CPTD, New Properties / Sessions, Traveler Actions / Sessions, New Properties / Clicks, Total Traveler Actions / Clicks, Leads / Clicks, Purchase / Clicks, FF_BRSubmit_Event_Count / Clicks, Impression Share, New Properties / Sessions
derived_range_start_row = 12  # Same start row as primary metric ranges
derived_range_start_column = 102  # Adjust the starting column for derived metrics
derived_spacing_between_ranges = 5  # Same column spacing as primary metric ranges

# Loop through derived metrics to create corresponding ranges
try:
    for derived_metric in derived_metrics:
        # Split the derived metric formula to get the two base metrics
        base_metrics = derived_metric.split(" / ")
        metric_1 = base_metrics[0].strip()
        metric_2 = base_metrics[1].strip()
        
        # Generate unique derived range name based on metrics
        derived_range_name = f"{metric_1}_over_{metric_2}_Range"
        
        # Pivot table names corresponding to the metrics
        pivot_table_name_1 = f"PivotTable_{metric_1}"
        pivot_table_name_2 = f"PivotTable_{metric_2}"
        
        # Run VBA macro to create derived metric ranges
        macro_name = f"{final_workbook.Name}!DerivedRangeModule.CreateDerivedRange"
        
        # Call VBA to generate the derived range for these base metrics
        excel_app.Application.Run(macro_name, pivot_table_name_1, pivot_table_name_2, metric_1, metric_2, derived_range_name, derived_range_start_row, derived_range_start_column)
        
        # Insert metric name two rows above the range
        wsRange = final_workbook.Sheets("Pivot_Sheet")
        wsRange.Cells(derived_range_start_row -5, derived_range_start_column).Value = derived_metric
        
        # Adjust the column position for the next derived metric
        derived_range_start_column += derived_spacing_between_ranges
        print(f"Derived range created successfully for {derived_metric}")
except Exception as e:
    print(f"Error while creating derived metric range: {e}")

# ------------------------------ CLEAN DATA RANGES FROM 0, #DIV/0, -100% ------------------------------ #

# Select the worksheet where your ranges are located
ws = final_workbook.Sheets("Pivot_Sheet")

# Loop through the used range in the worksheet
used_range = ws.UsedRange

# Iterate over all rows and columns within the used range
for row in used_range.Rows:
    for cell in row.Columns:
        try:
            # Check if the cell contains a value (not empty)
            value = cell.Value

            # Check if the value is #DIV/0!
            if isinstance(value, str) and value == "#DIV/0!":
                cell.ClearContents()

            # Check if the value is 0 or -100%
            elif isinstance(value, (int, float)) and (value == 0 or value == -1 or value == -100):
                cell.ClearContents()

        except Exception as e:
            # Log any exceptions or errors encountered during processing
            print(f"Error processing cell: {cell.Address}, {str(e)}")
            continue

# # ------------------------------ INSERT VBA CODE FOR SHARED PIVOT CACHE ------------------------------ #
# try:
#     # Insert the VBA code for shared cache pivot tables
#     new_module = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
#     new_module.Name = "SharedPivotCacheModule"
#     new_module.CodeModule.AddFromString(vba_code_shared_cache)
#     print("VBA code for shared pivot cache successfully added to the final report.")
# except Exception as e:
#     print(f"Error while adding shared pivot cache VBA code: {e}")

# # ------------------------------ EXECUTE VBA MACRO ------------------------------ #

# # Define the metrics list
# metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "Total Traveler Actions", "FF_BRSubmit_Event_Count", "Cost", "Sessions", "Clicks", "Impressions", "Impression_population"]

# # Call the macro with the metrics as argument
# try:
#     excel_app.Application.Run(f"{final_workbook.Name}!SharedPivotCacheModule.CreatePivotTablesWithSharedCache", metrics)
#     print("VBA macro executed successfully.")
# except Exception as e:
#     print(f"Error while executing VBA macro: {e}")

# ------------------------------ INSERT SLICERS FOR ALL PIVOT TABLES ------------------------------ #

# Insert columns before the first pivot table
def insert_columns_and_add_slicers():
    # Get the worksheet where the pivot tables are located
    ws = final_workbook.Sheets("Pivot_Sheet")

    # Insert 52 columns before the first pivot table (adjust column index as necessary)
    ws.Columns("A:AZ").Insert()

    print("52 columns inserted for dashboard space.")
    
    # Metrics list from the earlier part of the script
    metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count", "Total Traveler Actions", "FF_BRSubmit_Event_Count", "Cost", "Sessions", "Clicks", "Impressions", "Impression_population"]
    
    # Dynamically generate pivot table names using the metrics list
    pivot_tables = [ws.PivotTables(f"PivotTable_{metric}") for metric in metrics]

    # Now, add slicers for the relevant fields
    try:
        # Slicers to be placed in the left column
        left_slicer_fields = ["Paid or Non-Paid", "Paid Media Type", "Brand or Non-Brand", "Ad Platform", "Channel_Group", "Campaign_Group"]

        # Slicers to be positioned horizontally on top
        top_slicer_fields = ["Tenant or Landlord", "Total", "Period", "ISO_Year"]

        # Access SlicerCaches to add slicers
        slicer_left_position = 10  # Adjust the left position for left column slicers
        slicer_top_position = 10   # Adjust the top position for left column slicers
        slicer_height = 100        # Adjust slicer height as needed
        
        slicer_caches = []  # Store slicer caches to connect them to all pivot tables

        # Add slicers to the left column
        for slicer_field in left_slicer_fields:
            slicer_cache = final_workbook.SlicerCaches.Add2(pivot_tables[0], slicer_field)
            slicer = slicer_cache.Slicers.Add(ws)
            slicer.Top = slicer_top_position
            slicer.Left = slicer_left_position
            slicer.Height = slicer_height

            slicer_top_position += slicer_height + 10  # Adjust vertical spacing between slicers
            slicer_caches.append(slicer_cache)  # Append to the list to be used later

        # Position slicers horizontally on top of the worksheet
        slicer_top_position = 10  # Reset the top position for the top slicers
        slicer_left_position = 160  # Adjust left position for top slicers

        for slicer_field in top_slicer_fields:
            slicer_cache = final_workbook.SlicerCaches.Add2(pivot_tables[0], slicer_field)
            slicer = slicer_cache.Slicers.Add(ws)
            slicer.Top = slicer_top_position
            slicer.Left = slicer_left_position
            slicer.Height = slicer_height

            slicer_left_position += slicer.Height + 40  # Adjust horizontal spacing between slicers
            slicer_caches.append(slicer_cache)  # Append to the list

            # Set default filters for "Total" and "Period" slicers
            if slicer_field == "Total":
                slicer_cache.SlicerItems("Non-Total").Selected = True
                slicer_cache.SlicerItems("Total").Selected = False

            if slicer_field == "Period":
                slicer_cache.SlicerItems("WTD").Selected = True
                for item in slicer_cache.SlicerItems:
                    if item.Name != "WTD":
                        item.Selected = False

        # Connect slicers to all pivot tables in the same worksheet
        for slicer_cache in slicer_caches:
            for pt in pivot_tables:
                try:
                    slicer_cache.PivotTables.AddPivotTable(pt)  # Connect slicer to each pivot table
                except Exception as e:
                    print(f"Error connecting slicer to pivot table {pt.Name}: {e}")

        print(f"Slicers for {left_slicer_fields + top_slicer_fields} added successfully and connected to all pivot tables.")

    except Exception as e:
        print(f"Error while adding slicers: {e}")

# Execute the function after creating the pivot tables
insert_columns_and_add_slicers()

# --------------------------------- VBA CODE TO CREATE CHART PREFIX --------------------------------- #

# Insert the VBA code for creating the filter and chart prefix
try:
    new_module_filter_info = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module_filter_info.Name = "FilterInfoModule"
    new_module_filter_info.CodeModule.AddFromString(vba_code_chart_prefix)
    print("VBA code for filter information successfully added.")

    # Execute the filter information creation
    excel_app.Application.Run(f"{final_workbook.Name}!FilterInfoModule.CreateFilterInformation")

except Exception as e:
    print(f"Error while creating filter information: {e}")

# --------------------------------- VBA CODE TO CREATE CHART VISUALIZATIONS --------------------------------- #

# Insert the VBA module and code
try:
    new_module = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module.Name = "ChartCreationModule"
    new_module.CodeModule.AddFromString(vba_code_chart_creation)
    print("VBA code for chart creation added successfully.")
except Exception as e:
    print(f"Error while adding VBA code: {e}")

# Run the VBA macro to create the charts
try:
    macro_name = f"{final_workbook.Name}!ChartCreationModule.CreateLandlordCharts"
    excel_app.Application.Run(macro_name)
    print("Charts created successfully.")
except Exception as e:
    print(f"Error while running the chart creation macro: {e}")


# Save and close the final report workbook
final_workbook.Save()

# Close the Excel application
final_workbook.Close(True)
excel_app.Quit()

print("Process completed successfully.")