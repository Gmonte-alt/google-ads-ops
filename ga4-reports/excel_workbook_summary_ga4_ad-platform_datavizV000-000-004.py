# file name: ga4-reports/excel_workbook_summary_ga4_ad-platform_dataviz.py
# version: V000-000-004
# Notes: Second column of charts - YoY comparison according to specified groupings

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

# Define the enhanced VBA code with dynamic parameters. BA Code for First Column Charts
vba_code_chart1 = """
Sub CreateCombinationChart(metricName As String, chartNumber As Integer, topPosition As Double)
    Dim ws As Worksheet
    Dim chartSheet As Worksheet
    Dim chartObj As ChartObject
    Dim myChart As Chart
    Dim seriesCount As Integer
    Dim i As Integer
    Dim dataRange As Range
    Dim isoWeeksRange As Range
    Dim metricColumn As Range
    Dim yearRange As Range
    Dim campaignGroupRange As Range
    Dim filteredRange As Range
    Dim currentYear As Integer
    Dim campaignGroup As String
    Dim seriesXValues As Range
    Dim seriesValues As Range
    Dim row As Range ' New variable to represent each row in filteredRange
    
    ' Set your worksheet containing the data
    Set ws = ThisWorkbook.Sheets("--DATA--WTD--")
    
    ' Define the range for ISO Weeks (X-axis values)
    Set isoWeeksRange = ws.Range(ws.Cells(1, 5), ws.Cells(1, 56)) ' Columns E to BD, Row 1
    Debug.Print "ISO Weeks Range: " & isoWeeksRange.Address
    
    ' Define the range for the metric provided dynamically
    Set metricColumn = ws.Range("A:A") ' Assuming metric is in column A
    Set yearRange = ws.Range("B:B") ' Assuming Year is in column B
    Set campaignGroupRange = ws.Range("D:D") ' Assuming Campaign_Group is in column D

    ' Initialize filteredRange
    Set filteredRange = Nothing

    ' Determine groupings based on chartNumber
    Select Case chartNumber
        Case 1
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Non-Paid Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Grand Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 2
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Non-Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Social + Display Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Paid Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 3
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Direct") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Organic Search (SEO)")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 4
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google Search - TNH Non-Brand") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Bing SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Paid Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
    End Select

    ' Debugging: Print filtered range address after loop
    If Not filteredRange Is Nothing Then
        Debug.Print "Filtered Range: " & filteredRange.Address
    Else
        Debug.Print "No rows matched the criteria."
    End If

    ' If there is no data to chart, exit the sub
    If filteredRange Is Nothing Then
        MsgBox "No data available for the specified filters.", vbExclamation
        Exit Sub
    End If

    ' Check if the sheet "WTD_CHART" exists, and create it if it doesn't
    On Error Resume Next
    Set chartSheet = ThisWorkbook.Sheets("WTD_CHART")
    On Error GoTo 0
    If chartSheet Is Nothing Then
        Set chartSheet = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        chartSheet.Name = "WTD_CHART"
    End If

    ' Create the chart on the "WTD_CHART" worksheet at the provided top position
    Set chartObj = chartSheet.ChartObjects.Add(Left:=50, Width:=800, Top:=topPosition, Height:=400)
    chartObj.Name = "Combination Chart_" & metricName & "_" & chartNumber
    Set myChart = chartObj.Chart
    
    ' Set the chart type to Column Clustered initially
    myChart.ChartType = xlColumnClustered
    
    ' Add each series to the chart from the filtered range
    Dim rowIndex As Integer
    rowIndex = 1 ' Initialize row index for debugging
    
    For Each row In filteredRange.Rows
        ' Define series XValues and Values ranges
        Set seriesXValues = isoWeeksRange
        Set seriesValues = row.Cells(1, 5).Resize(1, 52) ' Columns E to BD for the current row
        
        ' Debugging: Print series ranges
        Debug.Print "Series " & rowIndex & " XValues: " & seriesXValues.Address
        Debug.Print "Series " & rowIndex & " Values: " & seriesValues.Address
        
        ' Check if seriesValues contains valid data before adding
        If WorksheetFunction.Count(seriesValues) > 0 Then
            With myChart.SeriesCollection.NewSeries
                .Name = row.Cells(1, 4).Value & " " & row.Cells(1, 2).Value ' Campaign_Group + Year
                .XValues = seriesXValues
                .Values = seriesValues
                
                ' Determine the chart type for each series
                If row.Cells(1, 2).Value = 2024 Then
                    .ChartType = xlColumnStacked ' Use Stacked Column for 2024 data
                    .AxisGroup = xlPrimary ' Set all 2024 data on the primary axis
                ElseIf row.Cells(1, 2).Value = 2023 Then
                    .ChartType = xlLine ' Use Line for 2023 Grand Total
                    .AxisGroup = xlPrimary ' Use primary axis for consistent comparison
                End If
            End With
        Else
            Debug.Print "Series " & rowIndex & " contains no data and will not be added."
        End If
        rowIndex = rowIndex + 1 ' Increment row index for debugging
    Next row
    
    ' Set chart title and axis titles
    myChart.HasTitle = True
    myChart.ChartTitle.Text = "Combination Chart for " & metricName & " - Grouping " & chartNumber
    
    With myChart.Axes(xlCategory)
        .HasTitle = True
        .AxisTitle.Text = "ISO Weeks"
    End With
    
    With myChart.Axes(xlValue)
        .HasTitle = True
        .AxisTitle.Text = "Event Count"
    End With
    
    ' Adjust the primary axis group for all series
    For i = 1 To myChart.SeriesCollection.Count
        myChart.SeriesCollection(i).AxisGroup = xlPrimary
    Next i
    
    ' Optional: Adjust the chart legend and display properties
    myChart.HasLegend = True
    myChart.Legend.Position = xlLegendPositionBottom
    
    ' Optional: Customize the chart appearance as needed
    
    MsgBox "Combination Chart " & chartNumber & " for " & metricName & " created successfully!", vbInformation
End Sub
"""

# Define the new VBA code for the second column of charts
# VBA Code for Second Column Charts
vba_code_chart2 = """
Sub CreateSecondColumnCharts(metricName As String, chartNumber As Integer, leftPosition As Double, topPosition As Double)
    Dim ws As Worksheet
    Dim chartSheet As Worksheet
    Dim chartObj As ChartObject
    Dim myChart As Chart
    Dim filteredRange As Range
    Dim seriesXValues As Range
    Dim seriesValues As Range

    ' Set your worksheet containing the data
    Set ws = ThisWorkbook.Sheets("--DATA--WTD--")
    
    ' Define the range for ISO Weeks (X-axis values)
    Set isoWeeksRange = ws.Range(ws.Cells(1, 5), ws.Cells(1, 56)) ' Columns E to BD, Row 1
    Debug.Print "ISO Weeks Range: " & isoWeeksRange.Address

    ' Initialize filteredRange
    Set filteredRange = Nothing

    ' Determine groupings based on chartNumber
    Select Case chartNumber
        Case 1
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Non-Paid Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 2
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Total") Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 3
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Organic Search (SEO)") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Direct")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 4
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = metricName And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Bing SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google Search - TNH Non-Brand")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
    End Select

    ' Debugging: Print filtered range address after loop
    If Not filteredRange Is Nothing Then
        Debug.Print "Filtered Range: " & filteredRange.Address
    Else
        Debug.Print "No rows matched the criteria."
    End If

    ' If there is no data to chart, exit the sub
    If filteredRange Is Nothing Then
        MsgBox "No data available for the specified filters.", vbExclamation
        Exit Sub
    End If

    ' Check if the sheet "WTD_CHART" exists, and create it if it doesn't
    On Error Resume Next
    Set chartSheet = ThisWorkbook.Sheets("WTD_CHART")
    On Error GoTo 0
    If chartSheet Is Nothing Then
        Set chartSheet = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        chartSheet.Name = "WTD_CHART"
    End If

    ' Create the chart on the "WTD_CHART" worksheet at the provided top and left positions
    Set chartObj = chartSheet.ChartObjects.Add(Left:=leftPosition, Width:=800, Top:=topPosition, Height:=400)
    chartObj.Name = "Combination Chart_" & metricName & "_" & chartNumber
    Set myChart = chartObj.Chart

    ' Set the chart type to Column Clustered initially
    myChart.ChartType = xlColumnClustered
    
    ' Add each series to the chart from the filtered range
    Dim rowIndex As Integer
    rowIndex = 1 ' Initialize row index for debugging
    
    For Each row In filteredRange.Rows
        ' Define series XValues and Values ranges
        Set seriesXValues = isoWeeksRange
        Set seriesValues = row.Cells(1, 5).Resize(1, 52) ' Columns E to BD for the current row
        
        ' Debugging: Print series ranges
        Debug.Print "Series " & rowIndex & " XValues: " & seriesXValues.Address
        Debug.Print "Series " & rowIndex & " Values: " & seriesValues.Address
        
        ' Check if seriesValues contains valid data before adding
        If WorksheetFunction.Count(seriesValues) > 0 Then
            With myChart.SeriesCollection.NewSeries
                .Name = row.Cells(1, 4).Value & " " & row.Cells(1, 2).Value ' Campaign_Group + Year
                .XValues = seriesXValues
                .Values = seriesValues
            End With
        Else
            Debug.Print "Series " & rowIndex & " contains no data and will not be added."
        End If
        rowIndex = rowIndex + 1 ' Increment row index for debugging
    Next row

    ' Set chart title and axis titles
    myChart.HasTitle = True
    myChart.ChartTitle.Text = "Combination Chart for " & metricName & " - Grouping " & chartNumber

    With myChart.Axes(xlCategory)
        .HasTitle = True
        .AxisTitle.Text = "ISO Weeks"
        .TickLabelPosition = xlLow  ' Set X-axis label position to Low
    End With

    With myChart.Axes(xlValue)
        .HasTitle = True
        .AxisTitle.Text = "Event Count"
    End With

End Sub
"""

# Open Excel application
excel_app = win32.Dispatch("Excel.Application")

# Set Excel to be visible (optional)
excel_app.Visible = True

# Open the Final Report Workbook
final_workbook = excel_app.Workbooks.Open(final_report_path)

# Insert the VBA code for first column charts
try:
    new_module1 = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module1.Name = "DynamicChartModule"
    new_module1.CodeModule.AddFromString(vba_code_chart1)
    print("VBA code for first column charts successfully added to the final report.")
except Exception as e:
    print(f"Error while adding first column VBA code: {e}")

# Insert the VBA code for second column charts
try:
    new_module2 = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module2.Name = "SecondColumnChartsModule"
    new_module2.CodeModule.AddFromString(vba_code_chart2)
    print("VBA code for second column charts successfully added to the final report.")
except Exception as e:
    print(f"Error while adding second column VBA code: {e}")

# Define the metrics and their top positions for first column charts
metrics = ["FF_Purchase_Event_Count", "FF_Lead_Event_Count"]
chart_top_position = 20  # Start position for the first chart
chart_height = 400  # Height of each chart

# Run first column charts
try:
    for metric in metrics:
        for chart_num, top_position in enumerate([chart_top_position, chart_top_position + chart_height, 
                                                  chart_top_position + 2 * chart_height, chart_top_position + 3 * chart_height], start=1):
            macro_name = f"{final_workbook.Name}!DynamicChartModule.CreateCombinationChart"
            excel_app.Application.Run(macro_name, metric, chart_num, top_position)
            print(f"Chart for {metric} (Case {chart_num}) created successfully.")
        # Adjust the top position for the next metric
        chart_top_position += 4 * (chart_height + 20)  # Space between charts
except Exception as e:
    print(f"Error while creating first column chart: {e}")

# Define the metrics and their positions for second column charts
metrics = ["FF_Purchase_Event_Count_YoY", "FF_Lead_Event_Count_YoY"]
left_position = 900  # Position the charts to the right of the first set of charts
chart_top_position = 20  # Start position for the first chart in the second column

# Run second column charts
try:
    for metric in metrics:
        for chart_num, top_position in enumerate([chart_top_position, chart_top_position + chart_height, 
                                                  chart_top_position + 2 * chart_height, chart_top_position + 3 * chart_height], start=1):
            macro_name = f"{final_workbook.Name}!SecondColumnChartsModule.CreateSecondColumnCharts"
            excel_app.Application.Run(macro_name, metric, chart_num, left_position, top_position)
            print(f"Chart for {metric} (Case {chart_num}) created successfully.")
        # Adjust the top position for the next metric
        chart_top_position += 4 * (chart_height + 20)  # Space between charts
except Exception as e:
    print(f"Error while creating second column chart: {e}")

# Save and close the final report workbook
final_workbook.Save()

# Close the Excel application
final_workbook.Close(True)
excel_app.Quit()

print("Process completed successfully.")